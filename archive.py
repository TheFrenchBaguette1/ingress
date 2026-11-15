import logging
import re
from collections import OrderedDict
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from astropy import units as u
from astropy.io import fits
from astropy.table import Table
from astropy.time import Time
from astropy.utils.data import download_file
from astropy.wcs import WCS
from astroquery.ipac.irsa import IrsaClass
from requests import Session
from requests.exceptions import ConnectionError, Timeout

import config


class ArchiveUnavailable(RuntimeError):
    """IRSA's image search is temporarily unavailable."""


ARCHIVE_QUERY_BUDGET_SECONDS = 32
ARCHIVE_READ_TIMEOUT_SECONDS = 6
FALLBACK_WINDOW_SECONDS = 3.25
MAX_FALLBACK_WINDOWS = 4
# A broad square can produce an expensive SIA index query. A small circle at
# the entered coordinates often finds real parent exposures promptly; its
# listing is only a sample of the wider field and must be marked incomplete.
CENTRAL_FALLBACK_RADIUS_ARCSEC = 75.0
CENTRAL_FALLBACK_TIMEOUT_SECONDS = 4.0
TAP_FALLBACK_TIMEOUT_SECONDS = 20.0
TAP_FAILURE_COOLDOWN_SECONDS = 90.0
TAP_RESULT_TTL_SECONDS = 15 * 60
TAP_MAX_ROWS = 512
OUTAGE_COOLDOWN_SECONDS = 15
KNOWN_SOURCE_RETRY_SECONDS = 20
MAX_RECENT_FAILURES = 64
_outage_lock = Lock()
_recent_failure_lock = Lock()
_recent_failures = OrderedDict()
_tap_lock = Lock()
_tap_probe_lock = Lock()
_tap_results = OrderedDict()
_tap_failure_until = 0.0
_outage_until = 0.0
_outage_probe = False
_outage_message = (
    "IRSA's SPHEREx image search is temporarily unavailable. "
    "Please try again shortly."
)
_timeout_message = (
    "IRSA's SPHEREx image search did not respond in time for this patch. "
    "Try a shorter date range or a smaller field."
)


def _check_recent_outage():
    """Permit one probe after a confirmed outage's short cooling period."""
    global _outage_probe
    with _outage_lock:
        if _outage_until:
            if monotonic() < _outage_until or _outage_probe:
                raise ArchiveUnavailable(_outage_message)
            _outage_probe = True
            return True
    return False


def _record_outage():
    global _outage_until, _outage_probe
    with _outage_lock:
        _outage_until = monotonic() + OUTAGE_COOLDOWN_SECONDS
        _outage_probe = False


def _finish_probe(was_probe):
    global _outage_until, _outage_probe
    if was_probe:
        with _outage_lock:
            if _outage_probe:
                # An answer, including an empty listing, proves the archive
                # can respond. Also release the probe after a distinct error.
                _outage_until = 0.0
                _outage_probe = False


def _recent_failure_key(coord, radius, collections, start, end, band):
    return (float(coord.ra.deg), float(coord.dec.deg), float(radius),
            tuple(collections), start.isot, end.isot, band)


def _recently_failed(key):
    with _recent_failure_lock:
        until = _recent_failures.get(key, 0)
        if monotonic() >= until:
            _recent_failures.pop(key, None)
            return False
        return True


def _remember_failed_index(key):
    with _recent_failure_lock:
        _recent_failures[key] = monotonic() + KNOWN_SOURCE_RETRY_SECONDS
        _recent_failures.move_to_end(key)
        if len(_recent_failures) > MAX_RECENT_FAILURES:
            _recent_failures.popitem(last=False)


class _TimedSession(Session):
    """Bound PyVO's requests; its default SIA session has no read timeout."""

    deadline = None
    read_timeout_seconds = ARCHIVE_READ_TIMEOUT_SECONDS

    def request(self, *args, **kwargs):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise Timeout("IRSA image-search time budget exhausted")
        kwargs["timeout"] = min(self.read_timeout_seconds, remaining)
        return super().request(*args, **kwargs)


class _BoundedIrsa(IrsaClass):
    def __init__(self, deadline, *, read_timeout_seconds=ARCHIVE_READ_TIMEOUT_SECONDS):
        # BaseVOQuery preserves a session supplied before its initialization.
        self._session = _TimedSession()
        self._session.deadline = deadline
        self._session.read_timeout_seconds = read_timeout_seconds
        super().__init__()


def _transient_archive_error(error):
    if isinstance(error, (ConnectionError, Timeout)):
        return True
    message = str(error).lower()
    return bool(re.search(r"\b(?:502|503|504)\b", message)) or any(
        phrase in message for phrase in
        (
            "proxy error", "bad gateway", "service unavailable",
            "gateway timeout", "read timed out", "readtimeout",
            "connecttimeout",
        )
    )


def _timed_out(error):
    message = str(error).lower()
    return isinstance(error, Timeout) or any(
        phrase in message for phrase in
        ("timed out", "readtimeout", "connecttimeout", "gateway timeout",
         "time budget exhausted")
    )


def _confirmed_service_unavailable(error):
    # A gateway 502 can be specific to one expensive query. A read timeout
    # likewise says nothing about another sky position. Only an explicit 503
    # response can activate the short service-wide circuit breaker.
    message = str(error).lower()
    return "503" in message or "service unavailable" in message


def _fallback_intervals(start_time, end_time):
    """Use all of a short interval, or sample four separated longer windows."""
    span = (end_time - start_time).to_value(u.day)
    if span <= 0:
        return []
    if span <= 60:
        count = min(MAX_FALLBACK_WINDOWS, max(1, int(span + 0.999)))
        return [
            (start_time + (span * i / count) * u.day,
             start_time + (span * (i + 1) / count) * u.day)
            for i in range(count)
        ]
    width = min(45.0, span / 5)
    last_start = span - width
    return [
        (start_time + (last_start * i / (MAX_FALLBACK_WINDOWS - 1)) * u.day,
         start_time + (last_start * i / (MAX_FALLBACK_WINDOWS - 1) + width) * u.day)
        for i in range(MAX_FALLBACK_WINDOWS)
    ]


def _query_once(coord, search_radius_arcsec, collections, start_time, end_time,
                band, deadline):
    client = _BoundedIrsa(deadline)
    kwargs = {
        "pos": (coord, search_radius_arcsec * u.arcsec),
        "collection": collections,
        "time": (start_time, end_time),
    }
    if band is not None:
        low, high = config.DETECTOR_RANGES_UM[band]
        kwargs["band"] = (low * u.um, high * u.um)
    return client.query_sia(**kwargs)


def _tap_lookup(coord, collections, start_time, end_time, band, deadline):
    """Try IRSA's official SPHEREx TAP metadata as a bounded point fallback.

    TAP uses an independent image index but the same archive host. It may be
    unavailable too. A point-in-polygon lookup cannot establish complete
    coverage for a requested square, even when its result is empty.
    """
    global _tap_failure_until
    key = (float(coord.ra.deg), float(coord.dec.deg), start_time.isot, end_time.isot)
    with _tap_lock:
        cached = _tap_results.get(key)
        if cached is not None and monotonic() - cached[1] < TAP_RESULT_TTL_SECONDS:
            rows = cached[0].copy(copy_data=True)
            _tap_results.move_to_end(key)
        elif monotonic() < _tap_failure_until:
            return None
        else:
            rows = None
    if rows is None:
        wait = max(0.0, min(deadline - monotonic(), TAP_FALLBACK_TIMEOUT_SECONDS))
        if wait <= 1 or not _tap_probe_lock.acquire(timeout=wait):
            return None
        try:
            with _tap_lock:
                cached = _tap_results.get(key)
                if cached is not None and monotonic() - cached[1] < TAP_RESULT_TTL_SECONDS:
                    rows = cached[0].copy(copy_data=True)
                elif monotonic() < _tap_failure_until:
                    return None
            if rows is None:
                remaining = deadline - monotonic()
                if remaining <= 1:
                    return None
                # Numeric SkyCoord/Time inputs and a fixed table name prevent
                # user strings from entering ADQL. One query serves all bands.
                adql = (
                    "SELECT a.uri, p.time_bounds_lower, p.energy_bandpassname "
                    "FROM spherex.artifact a JOIN spherex.plane p "
                    "ON a.planeid = p.planeid "
                    f"WHERE 1=CONTAINS(POINT('ICRS',{coord.ra.deg:.12f},"
                    f"{coord.dec.deg:.12f}),p.poly) "
                    f"AND p.time_bounds_lower >= {start_time.mjd:.9f} "
                    f"AND p.time_bounds_lower < {end_time.mjd:.9f} "
                    "ORDER BY p.time_bounds_lower"
                )
                try:
                    client = _BoundedIrsa(
                        min(deadline, monotonic() + TAP_FALLBACK_TIMEOUT_SECONDS),
                        read_timeout_seconds=TAP_FALLBACK_TIMEOUT_SECONDS,
                    )
                    result = client.query_tap(adql, maxrec=TAP_MAX_ROWS)
                    rows = result.to_table() if hasattr(result, "to_table") else result
                    if not isinstance(rows, Table) or not all(
                        column in rows.colnames for column in
                        ("uri", "time_bounds_lower", "energy_bandpassname")
                    ):
                        raise ValueError("Unexpected IRSA TAP metadata response")
                except Exception as error:
                    logging.warning("SPHEREx TAP metadata fallback unavailable: %s", error)
                    with _tap_lock:
                        _tap_failure_until = monotonic() + TAP_FAILURE_COOLDOWN_SECONDS
                    return None
                with _tap_lock:
                    _tap_failure_until = 0.0
                    _tap_results[key] = (rows.copy(copy_data=True), monotonic())
                    _tap_results.move_to_end(key)
                    if len(_tap_results) > 16:
                        _tap_results.popitem(last=False)
        finally:
            _tap_probe_lock.release()

    from saved_sources import _trusted_source

    releases = {str(collection).removeprefix("spherex_") for collection in collections}
    found = {}
    for row in rows:
        if str(row["energy_bandpassname"]) != f"SPHEREx-{band}":
            continue
        raw_url = str(row["uri"])
        url = raw_url if raw_url.startswith("https://") else (
            "https://irsa.ipac.caltech.edu/" + raw_url.lstrip("/")
        )
        path = urlsplit(url).path
        release = path.split("/")[4] if len(path.split("/")) > 4 else ""
        detector = band[1:]
        try:
            mjd = float(row["time_bounds_lower"])
        except (ValueError, TypeError):
            continue
        if (
            _trusted_source(url) and release in releases
            and re.search(rf"/{detector}/[^/]+\.fits$", path)
            and start_time.mjd <= mjd < end_time.mjd
        ):
            found[url] = (mjd, f"SPHEREx-{band}", url)
    if not found:
        return None
    selected = Table(
        rows=sorted(found.values()),
        names=("t_min", "energy_bandpassname", "access_url"),
    )
    selected.meta["archive_listing_partial"] = True
    selected.meta["archive_listing_window_note"] = (
        "The SIA image search did not answer. IRSA TAP identified these real "
        "DR1/QR3 parent FITS products overlapping the entered coordinate "
        "in this detector and date range. The point-only query may miss other "
        "exposures in the wider field; the cutouts retain the requested width."
    )
    return selected


def search_observations(
    coord, search_radius_arcsec, collections, start_time, end_time, band=None,
    known_sources=None,
):
    """Find observations; salvage date samples after a slow or failed search.

    A sampled answer must never be mistaken for complete date coverage. SIA's
    BAND parameter tests overlapping wavelengths, so the caller also filters
    on the exact detector name.
    """
    failure_key = _recent_failure_key(
        coord, search_radius_arcsec, collections, start_time, end_time, band
    )
    if known_sources is not None and _recently_failed(failure_key):
        saved = known_sources()
        if saved is not None and len(saved):
            return saved
    try:
        was_probe = _check_recent_outage()
    except ArchiveUnavailable:
        saved = known_sources() if known_sources is not None else None
        if saved is not None and len(saved):
            _remember_failed_index(failure_key)
            return saved
        raise
    try:
        deadline = monotonic() + ARCHIVE_QUERY_BUDGET_SECONDS
        errors = []
        try:
            return _query_once(
                coord, search_radius_arcsec, collections, start_time, end_time,
                band, min(deadline, monotonic() + ARCHIVE_READ_TIMEOUT_SECONDS),
            )
        except Exception as error:
            if not _transient_archive_error(error):
                raise
            errors.append(error)

        # A real image URL from a verified prior manifest can be cut out at
        # the requested FOV without the index. Prefer it immediately after
        # the first failed lookup; further SIA probing would delay the user.
        saved = known_sources() if known_sources is not None else None
        if saved is not None and len(saved):
            _remember_failed_index(failure_key)
            return saved

        if band is not None and search_radius_arcsec > 2 * CENTRAL_FALLBACK_RADIUS_ARCSEC:
            remaining = deadline - monotonic()
            if remaining > 0.1:
                try:
                    center = _query_once(
                        coord, CENTRAL_FALLBACK_RADIUS_ARCSEC, collections,
                        start_time, end_time, band,
                        min(deadline, monotonic() + CENTRAL_FALLBACK_TIMEOUT_SECONDS),
                    )
                    # SIA BAND is an overlap test. Adjacent detector rows
                    # alone are not evidence that this detector was found.
                    if len(center) and "energy_bandpassname" in center.colnames:
                        center = center[center["energy_bandpassname"] == f"SPHEREx-{band}"]
                    if len(center):
                        center.meta["archive_listing_partial"] = True
                        center.meta["archive_listing_window_note"] = (
                            "The full-field archive search did not answer. "
                            "These real exposures were found within 75 arcseconds "
                            "of the entered coordinates, with the selected detector "
                            "and date range. Other exposures in the wider field may "
                            "be missing; cutouts still use the requested field width."
                        )
                        return center
                except Exception as error:
                    if not _transient_archive_error(error):
                        raise
                    errors.append(error)

        if band is not None:
            # IRSA documents its SPHEREx TAP artifact/plane tables as another
            # way to find source FITS files. One bounded, shared point query
            # can recover real products when the SIA index is unresponsive.
            found = _tap_lookup(
                coord, collections, start_time, end_time, band, deadline,
            )
            if found is not None:
                return found

        from astropy.table import vstack

        intervals = _fallback_intervals(start_time, end_time)
        successes = []
        for window_start, window_end in intervals:
            remaining = deadline - monotonic()
            if remaining <= 0.1:
                break
            try:
                result = _query_once(
                    coord, search_radius_arcsec, collections, window_start,
                    window_end, band,
                    min(deadline, monotonic() + FALLBACK_WINDOW_SECONDS),
                )
                successes.append(result)
            except Exception as error:
                if not _transient_archive_error(error):
                    raise
                errors.append(error)

        if successes:
            combined = vstack(successes, metadata_conflicts="silent")
            complete = len(successes) == len(intervals) and (
                (end_time - start_time).to_value(u.day) <= 60
            )
            if complete or len(combined):
                if not complete:
                    combined.meta["archive_listing_partial"] = True
                    combined.meta["archive_listing_window_note"] = (
                        f"Archive search sampled {len(successes)} of "
                        f"{len(intervals)} shorter date windows after the "
                        "full-range lookup failed; dates outside successful "
                        "windows may be missing."
                    )
                return combined

        # Empty results from just a subset do not prove there is no coverage.
        # Failed 502 queries and timeouts are not proof of global service loss.
        if errors and all(_confirmed_service_unavailable(error) for error in errors):
            _record_outage()
        message = _timeout_message if any(_timed_out(error) for error in errors) else _outage_message
        raise ArchiveUnavailable(message) from errors[-1]
    finally:
        _finish_probe(was_probe)


def load_cutout(
    observation,
    coord,
    size_arcmin,
    cache=True,
    timeout_seconds=120,
):
    """Return a cutout's image, coordinate mapping, and metadata."""
    size_deg = (size_arcmin * u.arcmin).to_value(u.deg)
    image_url = str(observation["access_url"])

    cutout_url = (
        f"{image_url}?center={coord.ra.deg},{coord.dec.deg}d"
        f"&size={size_deg}"
    )

    # Each concurrent cutout needs its own network timeout. Changing Astropy's
    # global remote_timeout in a context manager races with other workers.
    local_file = download_file(
        cutout_url, cache=cache, show_progress=False, timeout=timeout_seconds
    )
    with fits.open(local_file) as hdul:
        image = hdul["IMAGE"].data.copy()
        header = hdul["IMAGE"].header
        spatial_wcs = WCS(header)

        x, y = spatial_wcs.world_to_pixel(coord)

        # Disable spatial SIP distortion only for the spectral mapping.
        spectral_wcs = WCS(header, fobj=hdul, key="W")
        spectral_wcs.sip = None

        wavelength, bandwidth = spectral_wcs.pixel_to_world(x, y)

        return {
            "image": image,
            "wcs": spatial_wcs,
            "target_x": float(x),
            "target_y": float(y),
            "wavelength_um": float(wavelength.to_value(u.um)),
            "bandwidth_um": float(bandwidth.to_value(u.um)),
            "brightness_unit": header.get("BUNIT", "MJy/sr"),
            "time_mjd": float(observation["t_min"]),
            "date_utc": Time(
                float(observation["t_min"]),
                format="mjd",
                scale="utc",
            ).iso,
            "detector": str(observation["energy_bandpassname"]),
            "source_url": image_url,
            "cutout_url": cutout_url,
        }
