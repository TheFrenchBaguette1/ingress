"""Reuse successful IRSA observation listings across detector-band searches."""

from collections import OrderedDict
from threading import Lock

from astropy.table import Table


MAX_CACHED_LISTINGS = 16
MAX_CACHED_ROWS = 5000
_lock = Lock()
_listings = OrderedDict()


def clear_archive_cache():
    """Clear the in-process listings (useful for isolated tests)."""
    with _lock:
        _listings.clear()


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
            return cached.copy(copy_data=True)

    results = fetch(coord, search_radius_arcsec, collections, start_time, end_time)
    if isinstance(results, Table) and len(results) <= MAX_CACHED_ROWS:
        with _lock:
            _listings[key] = results.copy(copy_data=True)
            _listings.move_to_end(key)
            if len(_listings) > MAX_CACHED_LISTINGS:
                _listings.popitem(last=False)
        return results.copy(copy_data=True)
    return results
