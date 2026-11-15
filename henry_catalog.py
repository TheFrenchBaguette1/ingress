"""Look up target-centered Level 2 exposures in independent IRSA collections.

The point CSV query and per-collection retry design come from Henry Lin's
Ingress backend, henrylin1039/Ingress commit ab96faf (backend/spherex.py).
"""

import csv
import hashlib
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from math import isfinite
from time import monotonic, sleep, time
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from astropy.table import Table

import config
from saved_sources import _trusted_source


ENDPOINT = "https://irsa.ipac.caltech.edu/SIA"
COLLECTIONS = ("spherex_dr1", "spherex_dr1_deep", "spherex_qr3", "spherex_qr3_deep")
MAX_ROWS = 5000
MAX_RESPONSE_BYTES = 12_000_000
MAX_ATTEMPTS = 3
CACHE_TTL_SECONDS = 6 * 3600
COLUMNS = ("t_min", "energy_bandpassname", "access_url")
CSV_COLUMNS = {*COLUMNS, "dataproduct_type"}
PRODUCT_PATH = re.compile(
    r"/ibe/data/spherex/(?P<release>dr1|qr3)/level2/[A-Za-z0-9_-]+/"
    r"[A-Za-z0-9_-]+/(?P<directory_detector>[1-6])/level2_"
    r"(?P<observation>[A-Za-z0-9_]+)D(?P<detector>[1-6])_spx_[A-Za-z0-9_-]+\.fits"
)
_executor = ThreadPoolExecutor(max_workers=len(COLLECTIONS), thread_name_prefix="spherex-point-sia")
_log = logging.getLogger(__name__)


def _product(url, detector, collection):
    if not _trusted_source(url):
        return None
    match = PRODUCT_PATH.fullmatch(urlsplit(url).path)
    if (match is None or detector != f"SPHEREx-D{match['detector']}"
            or match["directory_detector"] != match["detector"]
            or match["release"] != collection.split("_")[1]):
        return None
    return match["observation"], detector


def _valid_row(raw, collection, first, last):
    if len(raw) != 3:
        raise ValueError("Invalid SPHEREx catalog record")
    timestamp, detector, url = raw
    timestamp = float(timestamp)
    if not isfinite(timestamp) or _product(url, detector, collection) is None:
        raise ValueError("Invalid SPHEREx date, detector, or parent FITS URL")
    if first <= timestamp < last:
        return timestamp, detector, url
    return None


def _parse_csv(text, collection, first, last):
    reader = csv.DictReader(io.StringIO(text), strict=True)
    try:
        columns = reader.fieldnames
    except csv.Error:
        return None
    if not columns or len(set(columns)) != len(columns) or not CSV_COLUMNS.issubset(columns):
        return None
    rows = []
    incomplete = False
    count = 0
    try:
        for record in reader:
            count += 1
            if None in record or any(record[field] is None for field in CSV_COLUMNS):
                incomplete = True
                continue
            if record["dataproduct_type"] != "image":
                continue
            try:
                row = _valid_row(tuple(record[field] for field in COLUMNS), collection, first, last)
            except (TypeError, ValueError):
                incomplete = True
                continue
            if row is not None:
                rows.append(row)
    except csv.Error:
        incomplete = True
    return rows, not incomplete and count < MAX_ROWS


def _download(collection, ra, dec, first, last, deadline):
    params = {"COLLECTION": collection, "POS": f"circle {ra:.12f} {dec:.12f} 0.001",
              "TIME": f"{first:.9f} {last:.9f}", "RESPONSEFORMAT": "csv", "MAXREC": MAX_ROWS}
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - monotonic()
        if remaining < 0.2:
            break
        try:
            with requests.get(ENDPOINT, params=params, stream=True, allow_redirects=False,
                              timeout=(min(3, remaining), min(5, remaining))) as response:
                if response.status_code == 200:
                    payload = bytearray()
                    for block in response.iter_content(chunk_size=65536):
                        if monotonic() >= deadline or len(payload) + len(block) > MAX_RESPONSE_BYTES:
                            return None
                        payload.extend(block)
                    result = _parse_csv(payload.decode("utf-8-sig"), collection, first, last)
                    if result is not None:
                        return result
                    _log.info("IRSA returned an unrecognized %s catalog response", collection)
                elif response.status_code not in (408, 429, 500, 502, 503, 504):
                    return None
        except (requests.RequestException, UnicodeError) as error:
            _log.info("IRSA %s point catalog request failed: %s", collection, error)
        delay = 0.2 * (2 ** attempt)
        if attempt + 1 < MAX_ATTEMPTS and monotonic() + delay < deadline:
            sleep(delay)
    return None


def _cache_path(ra, dec, first, last):
    key = f"v1|{ra:.12f}|{dec:.12f}|{first:.9f}|{last:.9f}"
    return config.OUTPUT_ROOT / ".point-catalog" / hashlib.sha256(key.encode()).hexdigest()


def _read_cache(path, collection, first, last):
    file = path / f"{collection}.json"
    try:
        if file.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(file.read_text(encoding="utf-8"))
        saved_at = float(payload["saved_at"])
        if not isfinite(saved_at) or saved_at > time() or len(payload["rows"]) >= MAX_ROWS:
            return None
        rows = [_valid_row(row, collection, first, last) for row in payload["rows"]]
        if any(row is None for row in rows):
            return None
        return saved_at, rows
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _write_cache(path, collection, rows):
    destination = path / f"{collection}.json"
    temporary = path / f"{uuid4().hex}.tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({"saved_at": time(), "rows": rows}), encoding="utf-8")
        temporary.replace(destination)
    except OSError as error:
        _log.info("Could not save IRSA point catalog: %s", error)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def search_catalog(coord, first, last, *, deadline, refresh=False):
    """Return all detector exposures for the target point, with coverage metadata.

    Timestamps remain IRSA's start MJD for date filtering. The frame reader
    validates the exact target, image pixels, and FITS observation metadata.
    """
    ra, dec, first, last = float(coord.ra.deg), float(coord.dec.deg), float(first.mjd), float(last.mjd)
    cache = _cache_path(ra, dec, first, last)
    cached = {collection: _read_cache(cache, collection, first, last) for collection in COLLECTIONS}
    fresh = {collection for collection, entry in cached.items()
             if not refresh and entry is not None and time() - entry[0] < CACHE_TTL_SECONDS}
    futures = {collection: _executor.submit(_download, collection, ra, dec, first, last, deadline)
               for collection in COLLECTIONS if collection not in fresh and monotonic() < deadline}
    done, pending = wait(futures.values(), timeout=max(0, deadline - monotonic()))
    for future in pending:
        future.cancel()

    products = {}
    statuses = {}
    for collection in COLLECTIONS:
        entry = cached[collection]
        future = futures.get(collection)
        result = future.result() if future in done else None
        if result is not None and result[1]:
            rows, _ = result
            statuses[collection] = "ready"
            _write_cache(cache, collection, rows)
        elif collection in fresh:
            _, rows = entry
            statuses[collection] = "cached"
        elif entry is not None:
            _, saved_rows = entry
            rows = [*(result[0] if result is not None else []), *saved_rows]
            statuses[collection] = "stale"
        elif result is not None:
            rows, _ = result
            statuses[collection] = "partial"
        else:
            statuses[collection] = "unavailable"
            continue
        for row in sorted(rows, key=lambda item: item[0]):
            products.setdefault(_product(row[2], row[1], collection), row)

    states = set(statuses.values())
    if states == {"unavailable"}:
        return None
    table = Table(rows=sorted(products.values()), names=COLUMNS, dtype=(float, str, str))
    incomplete = states - {"ready", "cached"}
    table.meta.update({"archive_listing_scope": "target_point",
                       "archive_listing_collections": statuses,
                       "archive_listing_partial": bool(incomplete),
                       "archive_listing_stale": "stale" in states,
                       "archive_listing_cached": bool(states & {"cached", "stale"})})
    if "stale" in states:
        table.meta["archive_listing_window_note"] = (
            "Some IRSA collections did not return a complete listing. Verified saved rows are included; "
            "more exposures may be available."
        )
    elif incomplete:
        table.meta["archive_listing_window_note"] = (
            "Some IRSA collections returned incomplete listings or did not respond. "
            "More exposures may be available."
        )
    return table
