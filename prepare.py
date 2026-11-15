"""Bounded asynchronous preparation of six actual 20′ detector sequences.

One unfiltered SIA listing can serve all detectors. Each displayed frame comes
from an archive parent FITS product for its own detector.
"""

import copy
import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from math import sqrt
from threading import Lock, Thread
from time import monotonic, time
from typing import Literal
from uuid import uuid4

from astropy.coordinates import SkyCoord
from astropy.time import Time
from pydantic import BaseModel, ConfigDict, Field, model_validator

import config
from archive import ArchiveUnavailable, _query_once
from archive_cache import warm_all_band_listing
from sequence import SequenceRequest, build_sequence
from sequence_disk_cache import find_saved_sequence


PREPARE_BUDGET_SECONDS = 105
PREPARE_JOB_TTL_SECONDS = 15 * 60
MAX_JOBS = 16
BANDS = tuple(f"D{i}" for i in range(1, 7))
_jobs = OrderedDict()
_jobs_by_key = {}
_jobs_lock = Lock()


class PrepareRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    ra_deg: float = Field(ge=0, lt=360)
    dec_deg: float = Field(ge=-90, le=90)
    start_date: date
    end_date: date
    band: Literal["D1", "D2", "D3", "D4", "D5", "D6"]

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date or self.end_date == date.max:
            raise ValueError("Invalid preparation date interval.")
        return self


def _snapshot(job):
    return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})


def _update(job_id, *, band=None, state=None, manifest=None, error=None, done=False):
    with _jobs_lock:
        job = _jobs[job_id]
        if band is not None:
            item = {"status": state}
            if manifest is not None:
                item["manifest"] = manifest
            if error is not None:
                item["error"] = str(error)
            job["bands"][band] = item
        if done:
            for name, item in job["bands"].items():
                if item["status"] in {"queued", "running"}:
                    job["bands"][name] = {
                        "status": "unavailable",
                        "error": "Preparation ended before this band completed.",
                    }
            job["status"] = "complete"
        job["completed_count"] = sum(
            item["status"] in {"ready", "partial", "empty", "unavailable"}
            for item in job["bands"].values()
        )
        job["updated_at"] = time()
        return _snapshot(job)


def _retain_partial_or_unavailable(job_id, band, reason):
    """A failed refresh must not erase previously verified real frames."""
    with _jobs_lock:
        previous = _jobs[job_id].get("_fallbacks", {}).get(band)
    if previous and previous.get("manifest"):
        manifest = copy.deepcopy(previous["manifest"])
        note = f"Refresh failed; retained previously verified partial frames. {reason}"
        manifest["limitations"] = [note, *manifest.get("limitations", [])]
        return _update(job_id, band=band, state="partial", manifest=manifest, error=reason)
    return _update(job_id, band=band, state="unavailable", error=reason)


def _warm_listing(request, deadline, *, refresh=False):
    """Warm only a complete, exact-center, 20′ unfiltered SIA result."""
    remaining = deadline - monotonic()
    if remaining < 10:
        return
    start = max(request.start_date, config.MISSION_START_DATE)
    end = min(request.end_date + timedelta(days=1),
              datetime.now(timezone.utc).date() + timedelta(days=1))
    if start >= end:
        return
    coord = SkyCoord(request.ra_deg, request.dec_deg, unit="deg", frame="icrs")
    try:
        warm_all_band_listing(
            coord, 20 * 60 / sqrt(2), config.COLLECTIONS,
            Time(start.isoformat(), scale="utc"), Time(end.isoformat(), scale="utc"),
            # A single complete unfiltered probe. The long sampled-window
            # fallback belongs to individual detectors, not startup warmup.
            fetch=lambda center, radius, collections, first, last: _query_once(
                center, radius, collections, first, last, None, monotonic() + 8,
            ),
            refresh=refresh,
        )
    except Exception as error:
        # Band-specific queries and saved verified parents remain available.
        logging.warning("Shared SPHEREx band listing unavailable: %s", error)


def _prepare_band(job_id, request, band, deadline, *, refresh=False):
    if deadline - monotonic() < 8:
        return _retain_partial_or_unavailable(
            job_id, band, "The finite preparation budget was exhausted before this band began.",
        )
    _update(job_id, band=band, state="running")
    selected = SequenceRequest(
        ra_deg=request.ra_deg, dec_deg=request.dec_deg,
        start_date=request.start_date, end_date=request.end_date,
        fov_arcmin=20, band=band,
    )
    try:
        manifest = find_saved_sequence(selected, config.OUTPUT_ROOT, deadline=deadline)
        if manifest is None:
            manifest = build_sequence(selected, deadline=deadline, refresh=refresh)
        status = manifest["status"]
        if status not in {"ready", "partial", "empty"}:
            raise ValueError(f"Unexpected preparation status {status!r}")
        if status == "empty" and manifest.get("candidate_count", 0) > 0:
            return _retain_partial_or_unavailable(
                job_id, band, f"Found {manifest['candidate_count']} real {band} products, "
                "but no source FITS cutout could be read at 20′.",
            )
        if status == "empty":
            with _jobs_lock:
                had_partial = band in _jobs[job_id].get("_fallbacks", {})
            if had_partial:
                return _retain_partial_or_unavailable(
                    job_id, band, "No images appeared in the refreshed listing; "
                    "previously verified partial frames remain available.",
                )
        return _update(job_id, band=band, state=status, manifest=manifest)
    except ArchiveUnavailable as error:
        return _retain_partial_or_unavailable(job_id, band, str(error))
    except Exception as error:
        logging.exception("Could not prepare %s sky data.", band)
        return _retain_partial_or_unavailable(
            job_id, band, f"Could not prepare genuine {band} images: {type(error).__name__}: {error}",
        )


def _run_job(job_id, request, refresh):
    deadline = monotonic() + PREPARE_BUDGET_SECONDS
    try:
        _warm_listing(request, deadline, refresh=refresh)
        with _jobs_lock:
            pending = {band for band, item in _jobs[job_id]["bands"].items()
                       if item["status"] == "queued"}
        if request.band in pending:
            # Guarantee the requested view is prepared before other detectors
            # compete for FITS download and reprojection resources.
            _prepare_band(job_id, request, request.band, deadline, refresh=refresh)
        priority = tuple(band for band in BANDS if band in pending and band != request.band)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(_prepare_band, job_id, request, band, deadline,
                                      refresh=refresh)
                       for band in priority]
            for future in futures:
                future.result()
    except Exception:
        logging.exception("The SPHEREx six-band preparation failed unexpectedly.")
    finally:
        _update(job_id, done=True)


def start_prepare(request, *, refresh=False):
    key = json.dumps(request.model_dump(mode="json"), sort_keys=True)
    with _jobs_lock:
        existing_id = _jobs_by_key.get(key)
        existing = _jobs.get(existing_id)
        if existing is not None and (existing["status"] == "running"
                                     or not refresh and time() - existing["updated_at"] < PREPARE_JOB_TTL_SECONDS):
            return _snapshot(existing)
        preserved = {
            band: copy.deepcopy(item)
            for band, item in existing["bands"].items()
            if item["status"] == "ready"
        } if existing is not None and refresh else {}
        fallbacks = {
            band: copy.deepcopy(item)
            for band, item in existing["bands"].items()
            if item["status"] == "partial" and item.get("manifest")
        } if existing is not None and refresh else {}
        job_id = uuid4().hex
        job = {
            "job_id": job_id,
            "status": "running",
            "requested_band": request.band,
            "canonical_fov_arcmin": 20,
            "bands": {band: preserved.get(band, {"status": "queued"}) for band in BANDS},
            "completed_count": len(preserved),
            "total_count": len(BANDS),
            "updated_at": time(),
            "_key": key,
            "_fallbacks": fallbacks,
        }
        _jobs[job_id] = job
        _jobs_by_key[key] = job_id
        while len(_jobs) > MAX_JOBS:
            old_id, old = next(iter(_jobs.items()))
            if old["status"] == "running":
                break
            _jobs.pop(old_id)
            if _jobs_by_key.get(old["_key"]) == old_id:
                _jobs_by_key.pop(old["_key"])
        result = _snapshot(job)
    Thread(target=_run_job, args=(job_id, request, refresh), daemon=True,
           name=f"spherex-prepare-{job_id[:8]}").start()
    return result


def get_prepare(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job is not None else None
