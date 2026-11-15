"""Reuse successful IRSA observation listings across detector-band searches."""

import hashlib
import json
import math
import os
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from astropy.table import Table

import config


MAX_CACHED_LISTINGS = 16
MAX_CACHED_ROWS = 5000
MAX_DISK_BYTES = 4_000_000
DISK_TTL_SECONDS = 3600
_lock = Lock()
_listings = OrderedDict()


def clear_archive_cache():
    """Clear in-process listings; saved listings stay for restart tests."""
    with _lock:
        _listings.clear()


def _disk_path(key):
    digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode("utf-8")).hexdigest()
    return config.OUTPUT_ROOT / ".archive-listings" / f"{digest}.json"


def _read_disk_listing(key):
    path = _disk_path(key)
    try:
        if path.stat().st_size > MAX_DISK_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc).timestamp() - payload["saved_at"]
        rows = payload["rows"]
        if (
            payload.get("version") != 1 or payload.get("key") != json.loads(json.dumps(key))
            or not 0 <= age <= DISK_TTL_SECONDS
            or not isinstance(rows, list) or not 0 < len(rows) <= MAX_CACHED_ROWS
        ):
            return None
        if any(
            not isinstance(row, list) or len(row) != 3
            or not isinstance(row[0], (float, int)) or not math.isfinite(row[0])
            or not isinstance(row[1], str) or not row[1].startswith("SPHEREx-D")
            or not isinstance(row[2], str) or not row[2].startswith("https://")
            for row in rows
        ):
            return None
        listing = Table(rows=rows, names=("t_min", "energy_bandpassname", "access_url"))
        listing.meta["archive_listing_cached"] = True
        return listing
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _write_disk_listing(key, results):
    """Atomically save only the fields used to build a sequence."""
    if not all(name in results.colnames for name in
               ("t_min", "energy_bandpassname", "access_url")):
        return
    try:
        rows = [
            [float(row["t_min"]), str(row["energy_bandpassname"]), str(row["access_url"])]
            for row in results
        ]
        if not rows or any(
            not math.isfinite(row[0]) or not row[1].startswith("SPHEREx-D")
            or not row[2].startswith("https://") for row in rows
        ):
            return
        payload = {
            "version": 1, "key": list(key),
            "saved_at": datetime.now(timezone.utc).timestamp(), "rows": rows,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_DISK_BYTES:
            return
        path = _disk_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, KeyError):
        # A disk-cache failure must never fail an otherwise successful search.
        return


def cached_search_observations(
    coord, search_radius_arcsec, collections, start_time, end_time, fetch
):
    """Return a private copy of an exact sky/UTC query's observation table.

    The SIA listing includes all detector bands. A new band needs new FITS
    cutouts, but does not need another identical archive metadata request.
    Failed queries are never stored, and callers cannot mutate cache entries.
    """
    key = (
        float(coord.ra.deg), float(coord.dec.deg),
        float(search_radius_arcsec), tuple(collections),
        start_time.isot, end_time.isot,
    )
    with _lock:
        cached = _listings.get(key)
        if cached is not None:
            _listings.move_to_end(key)
            reused = cached.copy(copy_data=True)
            reused.meta["archive_listing_cached"] = True
            return reused

    saved = _read_disk_listing(key)
    if saved is not None:
        with _lock:
            _listings[key] = saved.copy(copy_data=True)
            _listings.move_to_end(key)
            if len(_listings) > MAX_CACHED_LISTINGS:
                _listings.popitem(last=False)
        return saved.copy(copy_data=True)

    results = fetch(coord, search_radius_arcsec, collections, start_time, end_time)
    if isinstance(results, Table) and 0 < len(results) <= MAX_CACHED_ROWS:
        with _lock:
            _listings[key] = results.copy(copy_data=True)
            _listings.move_to_end(key)
            if len(_listings) > MAX_CACHED_LISTINGS:
                _listings.popitem(last=False)
        _write_disk_listing(key, results)
        return results.copy(copy_data=True)
    return results
