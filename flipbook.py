"""Build a playable detector sequence while the remaining exposures load."""

import logging
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import isfinite, sqrt
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import uuid4

from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time
from pydantic import BaseModel, ConfigDict, Field, model_validator

import config
from archive import ArchiveUnavailable, CENTRAL_FALLBACK_RADIUS_ARCSEC, _query_once, search_observations
from archive_cache import MAX_CACHED_ROWS, cached_search_observations, warm_all_band_listing
from flipbook_render import PROGRESSIVE_PIPELINE, BandWriter, archive_product_matches_band
from henry_catalog import search_catalog
from saved_sources import find_saved_sources
from sequence import SequenceRequest
from sequence_disk_cache import find_saved_sequence


Band = Literal["D1", "D2", "D3", "D4", "D5", "D6"]
FlipbookSelection = Band | Literal["all"]
BANDS = tuple(config.DETECTOR_RANGES_UM)
FOV_ARCMIN = 20
MAX_CANDIDATES = 160
MAX_BAND_SECONDS = 210
CSV_CATALOG_SECONDS = 32
SHARED_QUERY_SECONDS = 6
CENTER_QUERY_SECONDS = 12
CUTOUT_TIMEOUT_SECONDS = 18
CUTOUTS_PER_BAND = 4
CUTOUT_WORKERS = 8
MAX_ACTIVE_BANDS = 12
MAX_PENDING_BANDS = 72
MAX_JOBS = 32
WARM_SECONDS = 15 * 60
FAILURE_REUSE_SECONDS = 15
PARTIAL_CATALOG_SECONDS = 120
INITIAL_FRAMES = 3
UPDATE_FRAMES = 6
UPDATE_SECONDS = 1.25
_ACTIVE = frozenset({"queued", "running"})

_band_executor = ThreadPoolExecutor(max_workers=MAX_ACTIVE_BANDS, thread_name_prefix="spherex-band")
_cutout_executor = ThreadPoolExecutor(max_workers=CUTOUT_WORKERS, thread_name_prefix="spherex-cutout")
_jobs_lock = Lock()
_jobs = OrderedDict()
_jobs_by_key = {}
_pending_bands = OrderedDict()
_running_bands = set()


class FlipbookBusy(RuntimeError):
    """The shared archive worker queue has no room for more work right now."""


class FlipbookRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    ra_deg: float = Field(ge=0, lt=360)
    dec_deg: float = Field(ge=-90, le=90)
    start_date: date
    end_date: date
    band: FlipbookSelection

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date or self.end_date == date.max:
            raise ValueError("Invalid flipbook date interval.")
        return self


@dataclass
class _Job:
    job_id: str
    key: tuple
    request: FlipbookRequest
    bands: dict = field(default_factory=lambda: {band: {"status": "idle"} for band in BANDS})
    finished: dict = field(default_factory=dict)
    revision: int = 0
    listing_lock: Lock = field(default_factory=Lock)
    listing_checked_at: float | None = None
    listing_refresh_at: float | None = None
    listing: Table | None = None


@dataclass(eq=False, frozen=True)
class _BandWork:
    job: _Job
    band: Band
    refresh: bool
    previous: dict
    requested_at: float


def _snapshot(job):
    entries = job.bands
    return {
        "job_id": job.job_id,
        "status": "running" if any(entry["status"] in _ACTIVE for entry in entries.values()) else "complete",
        "requested_band": job.request.band,
        "canonical_fov_arcmin": FOV_ARCMIN,
        "completed_count": sum(entry["status"] not in _ACTIVE and entry["status"] != "idle"
                               for entry in entries.values()),
        "total_count": len(BANDS),
        "revision": job.revision,
        "candidate_count": sum(entry.get("candidate_count", 0) for entry in entries.values()),
        "attempted_count": sum(entry.get("attempted_count", 0) for entry in entries.values()),
        "failure_count": sum(entry.get("failure_count", 0) for entry in entries.values()),
        "frame_count": sum((entry.get("manifest") or {}).get("frame_count", 0)
                           for entry in entries.values()),
        "bands": deepcopy(entries),
    }


def _publish(job, band, status, **fields):
    with _jobs_lock:
        job.bands[band] = {"status": status, **fields}
        job.revision += 1
        if status not in _ACTIVE:
            job.finished[band] = monotonic()


def _discard_old_job():
    for job_id, job in _jobs.items():
        if not any(entry["status"] in _ACTIVE for entry in job.bands.values()):
            _jobs.pop(job_id)
            if _jobs_by_key.get(job.key) == job_id:
                _jobs_by_key.pop(job.key)
            return
    raise FlipbookBusy("The archive queue is full. Try again in a moment.")


def _dispatch_pending():
    """Give each queued sky patch a detector turn as workers free up."""
    while _pending_bands and len(_running_bands) < MAX_ACTIVE_BANDS:
        job_id, queue = _pending_bands.popitem(last=False)
        task = queue.popleft()
        if queue:
            _pending_bands[job_id] = queue
        _running_bands.add(task)
        _band_executor.submit(_run_band, task)


def _run_band(task):
    try:
        _build_band(task.job, task.band, task.refresh, task.previous, task.requested_at)
    finally:
        with _jobs_lock:
            _running_bands.discard(task)
            _dispatch_pending()


def _schedule(job, selection, refresh):
    """Schedule under the registry lock to prevent duplicate builds."""
    now = monotonic()
    pending = []
    for band in BANDS if selection == "all" else (selection,):
        previous = job.bands[band]
        status = previous["status"]
        max_age = FAILURE_REUSE_SECONDS if status == "unavailable" else WARM_SECONDS
        if status in _ACTIVE:
            continue
        if status == "idle" or refresh or now - job.finished[band] >= max_age:
            pending.append((band, previous))
    if not pending:
        return
    waiting = sum(len(queue) for queue in _pending_bands.values())
    available_workers = MAX_ACTIVE_BANDS - len(_running_bands)
    if waiting + len(pending) > MAX_PENDING_BANDS + available_workers:
        raise FlipbookBusy("The archive queue is full. Try again in a moment.")
    queue = _pending_bands.setdefault(job.job_id, deque())
    for band, previous in pending:
        job.bands[band] = {**previous, "status": "queued"}
        job.revision += 1
        queue.append(_BandWork(job, band, refresh, previous, now))
    _dispatch_pending()


def start_flipbook(request, *, refresh=False):
    key = request.ra_deg, request.dec_deg, request.start_date, request.end_date
    with _jobs_lock:
        job_id = _jobs_by_key.get(key)
        job = _jobs.get(job_id)
        created = job is None
        if job is None:
            if len(_jobs) >= MAX_JOBS:
                _discard_old_job()
            job = _Job(uuid4().hex, key, request)
            _jobs[job.job_id] = job
            _jobs_by_key[key] = job.job_id
        _jobs.move_to_end(job.job_id)
        try:
            _schedule(job, request.band, refresh)
        except FlipbookBusy:
            if created:
                _jobs.pop(job.job_id)
                _jobs_by_key.pop(key)
            raise
        snapshot = _snapshot(job)
        snapshot["requested_band"] = request.band
        return snapshot


def get_flipbook(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        _jobs.move_to_end(job_id)
        return _snapshot(job)


def get_flipbook_exposures(job_id):
    """Read the target and the real parents already published to the viewer."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        _jobs.move_to_end(job_id)
        frames = tuple(
            frame for entry in job.bands.values()
            for frame in (entry.get("manifest") or {}).get("frames", ())
        )
        return job.request, frames


def request_flipbook_band(job_id, band, *, refresh=False):
    if band not in BANDS and band != "all":
        raise ValueError("Unknown SPHEREx detector band.")
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        _jobs.move_to_end(job_id)
        _schedule(job, band, refresh)
        snapshot = _snapshot(job)
        snapshot["requested_band"] = band
        return snapshot


def _listing_incomplete(table):
    status = str(table.meta.get("QUERY_STATUS", "")).upper()
    return bool(table.meta.get("archive_listing_partial") or status == "OVERFLOW"
                or len(table) >= MAX_CACHED_ROWS)


def _center_catalog(coord, first, last, band, deadline):
    if deadline - monotonic() <= 0:
        return None
    try:
        table = _query_once(
            coord, CENTRAL_FALLBACK_RADIUS_ARCSEC, config.COLLECTIONS, first, last, band,
            min(deadline, monotonic() + CENTER_QUERY_SECONDS),
            read_timeout_seconds=CENTER_QUERY_SECONDS,
        )
    except Exception as error:
        logging.info("SPHEREx center-only archive lookup did not finish: %s", error)
        return None
    if not isinstance(table, Table) or not len(table) or "energy_bandpassname" not in table.colnames:
        return None
    if band is not None and not any(str(item) == f"SPHEREx-{band}" for item in table["energy_bandpassname"]):
        return None
    table.meta["archive_listing_partial"] = True
    table.meta["archive_listing_window_note"] = (
        "The broad archive search did not finish. Verified exposures were found "
        "within 75 arcseconds of the target. The images still use the full requested sky patch."
    )
    return table


def _shared_catalog(job, coord, radius, first, last, deadline, refresh, scheduled_at):
    with job.listing_lock:
        if job.listing is None:
            lifetime = FAILURE_REUSE_SECONDS
        else:
            lifetime = PARTIAL_CATALOG_SECONDS if _listing_incomplete(job.listing) else WARM_SECONDS
        checked_at = job.listing_checked_at
        if checked_at is not None:
            if not refresh and monotonic() - checked_at < lifetime:
                return job.listing
            if refresh and job.listing_refresh_at is not None and job.listing_refresh_at >= scheduled_at:
                return job.listing
        if deadline - monotonic() <= 0:
            return None
        # Ask each release separately, with a small target-centered query.
        # This avoids the expensive combined search when IRSA is under load.
        try:
            table = search_catalog(
                coord, first, last, deadline=min(deadline, monotonic() + CSV_CATALOG_SECONDS),
                refresh=refresh,
            )
        except Exception as exc:
            logging.warning("SPHEREx per-collection lookup unavailable: %s", exc)
            table = None
        if table is None or not len(table) and _listing_incomplete(table):
            try:
                table = warm_all_band_listing(
                    coord, radius, config.COLLECTIONS, first, last,
                    fetch=lambda center, width, collections, start, end: _query_once(
                        center, width, collections, start, end, None,
                        min(deadline, monotonic() + SHARED_QUERY_SECONDS),
                    ),
                    refresh=refresh,
                )
            except Exception as exc:
                logging.info("SPHEREx shared archive lookup did not finish: %s", exc)
                table = None
            if not isinstance(table, Table) or _listing_incomplete(table):
                table = _center_catalog(coord, first, last, None, deadline)
        job.listing_checked_at = monotonic()
        job.listing_refresh_at = scheduled_at if refresh else None
        job.listing = table
        return job.listing


def _catalog(job, band, deadline, refresh, scheduled_at):
    request = job.request
    start = max(request.start_date, config.MISSION_START_DATE)
    end = min(request.end_date + timedelta(days=1),
              datetime.now(timezone.utc).date() + timedelta(days=1))
    if start >= end:
        return [], False, ()
    coord = SkyCoord(request.ra_deg, request.dec_deg, unit="deg", frame="icrs")
    first, last = Time(start.isoformat(), scale="utc"), Time(end.isoformat(), scale="utc")
    radius = FOV_ARCMIN * 60 / sqrt(2)
    table = _shared_catalog(job, coord, radius, first, last, deadline, refresh, scheduled_at)
    if (table is not None and _listing_incomplete(table)
            and "energy_bandpassname" in table.colnames
            and not any(str(item) == f"SPHEREx-{band}" for item in table["energy_bandpassname"])):
        table = None
    if table is None:
        def lookup(center, width, collections, start_time, end_time, *, band):
            narrowed = _center_catalog(center, start_time, end_time, band, deadline)
            if narrowed is not None:
                return narrowed
            return search_observations(
                center, width, collections, start_time, end_time, band=band,
            )

        try:
            table = cached_search_observations(
                coord, radius, config.COLLECTIONS, first, last, lookup,
                band=band, refresh=refresh,
            )
        except ArchiveUnavailable:
            table = find_saved_sources(coord, band, first, last)
            if table is None or not len(table):
                raise
    if not {"t_min", "energy_bandpassname", "access_url"} <= set(table.colnames):
        raise ValueError("IRSA's observation listing is missing required metadata.")
    candidates = {}
    rejected = 0
    first_mjd, last_mjd = float(first.mjd), float(last.mjd)
    for row in table:
        if str(row["energy_bandpassname"]) != f"SPHEREx-{band}":
            continue
        try:
            mjd = float(row["t_min"])
            if not isfinite(mjd):
                rejected += 1
                continue
            if not first_mjd <= mjd < last_mjd:
                continue
            url = str(row["access_url"])
            if archive_product_matches_band(url, band):
                # The same archive product may appear more than once in SIA.
                # A deterministic choice keeps the target metadata stable.
                candidates[url] = min(mjd, candidates.get(url, mjd))
            else:
                rejected += 1
        except (TypeError, ValueError):
            rejected += 1
    notes = []
    incomplete = _listing_incomplete(table) or rejected > 0
    window_note = table.meta.get("archive_listing_window_note")
    if isinstance(window_note, str) and window_note:
        notes.append(window_note)
    elif _listing_incomplete(table):
        notes.append("IRSA returned an incomplete observation listing. Additional dates may be available.")
    if table.meta.get("archive_listing_cached"):
        notes.append("Reused a saved observation listing. Refresh to check for recently released exposures.")
    if rejected:
        notes.append(f"{rejected} archive row(s) did not identify a verified SPHEREx parent FITS product.")
    rows = [{"t_min": mjd, "access_url": url, "energy_bandpassname": f"SPHEREx-{band}"}
            for url, mjd in sorted(candidates.items(), key=lambda item: (item[1], item[0]))]
    return rows, incomplete, tuple(notes)


def _candidate_plan(rows):
    """Lead with a few dates spread in time, then schedule the rest."""
    if not rows:
        return []
    if len(rows) > MAX_CANDIDATES:
        rows = [rows[round(i * (len(rows) - 1) / (MAX_CANDIDATES - 1))]
                for i in range(MAX_CANDIDATES)]
    count = min(CUTOUTS_PER_BAND, len(rows))
    if count <= 1:
        return rows
    first_time, last_time = rows[0]["t_min"], rows[-1]["t_min"]
    priority = []
    remaining = set(range(len(rows)))
    for i in range(count):
        target = first_time + i * (last_time - first_time) / (count - 1)
        index = min(remaining, key=lambda j: (abs(rows[j]["t_min"] - target), j))
        priority.append(index)
        remaining.remove(index)
    return [rows[i] for i in priority + sorted(remaining)]


def _saved_manifest(job, band, deadline, *, legacy_seed=False):
    request = job.request
    exact = SequenceRequest(
        ra_deg=request.ra_deg, dec_deg=request.dec_deg,
        start_date=request.start_date, end_date=request.end_date,
        fov_arcmin=FOV_ARCMIN, band=band,
    )
    saved = find_saved_sequence(
        exact, config.OUTPUT_ROOT, deadline=deadline, allow_backfill=False,
        pipeline="legacy" if legacy_seed else PROGRESSIVE_PIPELINE,
        allow_stale_partial=legacy_seed,
    )
    if saved and (legacy_seed or saved.get("cache_age_seconds", 0) < WARM_SECONDS):
        return saved
    return None


def _progress(manifest):
    return {name: manifest.get(name, 0) for name in
            ("candidate_count", "attempted_count", "failure_count")}


def _run_cutouts(job, band, writer, rows, listing_partial, notes, deadline, fallback):
    total = len(rows)
    plan = _candidate_plan(rows)
    pending = {}
    frames = []
    attempted = failures = cursor = published = 0
    seed_count = fallback.get("frame_count", 0) if fallback else 0
    due = None
    manifest = fallback

    def publish_running():
        nonlocal manifest, published, due
        latest = writer.manifest(
            frames, status="partial", candidate_count=total, attempted_count=attempted,
            failure_count=failures, limitations=notes, listing_partial=listing_partial,
            complete=False,
        )
        published = latest["frame_count"]
        if published >= seed_count:
            manifest = latest
        frames.clear()
        due = None
        _publish(job, band, "running", manifest=manifest, candidate_count=total,
                 attempted_count=attempted, failure_count=failures)

    def fill():
        nonlocal cursor
        while len(pending) < CUTOUTS_PER_BAND and cursor < len(plan):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            row = plan[cursor]
            cursor += 1
            future = _cutout_executor.submit(
                writer.align, row, min(CUTOUT_TIMEOUT_SECONDS, max(1, remaining))
            )
            pending[future] = row["access_url"]

    try:
        fill()
        while pending and monotonic() < deadline:
            now = monotonic()
            timeout = max(0, min(deadline, due if due is not None else deadline) - now)
            done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                source = pending.pop(future)
                attempted += 1
                try:
                    frames.append(future.result())
                    if due is None:
                        due = monotonic() + UPDATE_SECONDS
                except Exception as error:
                    failures += 1
                    logging.info("SPHEREx cutout rejected for %s: %s", source, error)
            fill()
            threshold = INITIAL_FRAMES if not published else UPDATE_FRAMES
            if pending and frames and (len(frames) >= threshold or monotonic() >= due):
                publish_running()
            elif not frames and done:
                fields = {"manifest": manifest} if manifest else {}
                _publish(job, band, "running", candidate_count=total, attempted_count=attempted,
                         failure_count=failures, **fields)
    finally:
        for future in pending:
            future.cancel()

    final_notes = list(notes)
    if len(plan) < total:
        final_notes.append(f"The viewer checks up to {MAX_CANDIDATES} exposures per detector; "
                           "the sampled products span the selected date range.")
    if attempted < len(plan):
        final_notes.append(f"The preparation time limit ended after checking {attempted} of "
                           f"{len(plan)} scheduled exposures.")
    if failures:
        final_notes.append(f"{failures} cutout(s) could not be used for this patch.")
    if not frames and not published:
        if not total and not listing_partial:
            return _finish_empty(job, band, final_notes, fallback)
        reason = ("No usable images were identified in the incomplete archive listing."
                  if not total else
                  f"No usable images could be built from {total} candidate exposures.")
        return _finish_unavailable(job, band, reason, fallback, total, attempted, failures)
    if published + len(frames) < seed_count:
        reason = (f"The new lookup prepared {published + len(frames)} usable exposures. "
                  f"The earlier sequence contains {seed_count} verified images.")
        return _finish_unavailable(job, band, reason, fallback, total, attempted, failures)
    status = "partial" if listing_partial or attempted < total or failures else "ready"
    manifest = writer.manifest(
        frames, status=status, candidate_count=total, attempted_count=attempted,
        failure_count=failures, limitations=final_notes, listing_partial=listing_partial,
    )
    _publish(job, band, status, manifest=manifest, candidate_count=total,
             attempted_count=attempted, failure_count=failures)


def _finish_empty(job, band, notes, fallback):
    message = "No observations from this detector cover the requested dates and sky patch."
    if fallback:
        return _finish_unavailable(job, band, message, fallback, 0, 0, 0)
    _publish(job, band, "empty", candidate_count=0, attempted_count=0, failure_count=0,
             error=" ".join([message, *notes]))


def _finish_unavailable(job, band, message, fallback, candidates=0, attempted=0, failures=0):
    if fallback:
        note = f"Refresh could not replace the previous verified frames. {message}"
        retained = {**fallback, "status": "partial",
                    "limitations": [note, *fallback.get("limitations", [])]}
        _publish(job, band, "partial", manifest=retained, error=message, **_progress(retained))
    else:
        _publish(job, band, "unavailable", error=message, candidate_count=candidates,
                 attempted_count=attempted, failure_count=failures)


def _build_band(job, band, refresh, previous, scheduled_at):
    deadline = monotonic() + MAX_BAND_SECONDS
    fallback = previous.get("manifest")
    try:
        _publish(job, band, "running", **({"manifest": fallback} if fallback else {}))
        if not fallback:
            saved = _saved_manifest(job, band, deadline) if not refresh else None
            if saved and saved["status"] == "ready":
                _publish(job, band, "ready", manifest=saved, **_progress(saved))
                return
            legacy = _saved_manifest(job, band, deadline, legacy_seed=True)
            seed = max((item for item in (saved, legacy) if item),
                       default=None, key=lambda item: item["frame_count"])
            if seed:
                fallback = {
                    **seed,
                    "status": "partial",
                    "limitations": [
                        "Showing verified saved frames while the archive checks for more exposures.",
                        *seed.get("limitations", []),
                    ],
                }
                _publish(job, band, "running", manifest=fallback, **_progress(fallback))
        rows, listing_partial, notes = _catalog(job, band, deadline, refresh, scheduled_at)
        if not rows and not listing_partial:
            _finish_empty(job, band, notes, fallback)
            return
        _publish(job, band, "running", candidate_count=len(rows), attempted_count=0,
                 failure_count=0, **({"manifest": fallback} if fallback else {}))
        writer = BandWriter(config.OUTPUT_ROOT, job.request, band)
        _run_cutouts(job, band, writer, rows, listing_partial, notes, deadline, fallback)
    except Exception as error:
        logging.warning("SPHEREx %s could not be prepared: %s", band, error, exc_info=True)
        with _jobs_lock:
            last = job.bands[band].get("manifest")
        _finish_unavailable(job, band, str(error), last or fallback)
