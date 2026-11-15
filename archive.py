import re
from threading import Lock
from time import monotonic, sleep

from astropy import units as u
from astropy.io import fits
from astropy.time import Time
from astropy.utils.data import download_file
from astropy.wcs import WCS
from astroquery.ipac.irsa import IrsaClass
from requests import Session
from requests.exceptions import ConnectionError, Timeout


class ArchiveUnavailable(RuntimeError):
    """IRSA's image search is temporarily unavailable."""


ARCHIVE_QUERY_BUDGET_SECONDS = 20
ARCHIVE_READ_TIMEOUT_SECONDS = 8
OUTAGE_COOLDOWN_SECONDS = 15
_outage_lock = Lock()
_outage_until = 0.0
_outage_probe = False
_outage_message = (
    "IRSA's SPHEREx image search is temporarily unavailable. "
    "Please try again shortly."
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


class _TimedSession(Session):
    """Bound PyVO's requests; its default SIA session has no read timeout."""

    deadline = None

    def request(self, *args, **kwargs):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise Timeout("IRSA image-search time budget exhausted")
        kwargs["timeout"] = min(ARCHIVE_READ_TIMEOUT_SECONDS, remaining)
        return super().request(*args, **kwargs)


class _BoundedIrsa(IrsaClass):
    def __init__(self, deadline):
        # BaseVOQuery preserves a session supplied before its initialization.
        self._session = _TimedSession()
        self._session.deadline = deadline
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


def search_observations(
    coord, search_radius_arcsec, collections, start_time, end_time
):
    """Find observations overlapping the region and requested UTC interval."""
    was_probe = _check_recent_outage()
    try:
        deadline = monotonic() + ARCHIVE_QUERY_BUDGET_SECONDS
        client = _BoundedIrsa(deadline)
        for attempt in range(2):
            try:
                return client.query_sia(
                    pos=(coord, search_radius_arcsec * u.arcsec),
                    collection=collections,
                    time=(start_time, end_time),
                )
            except Exception as error:
                if not _transient_archive_error(error):
                    raise
                if attempt == 1 or monotonic() >= deadline:
                    _record_outage()
                    raise ArchiveUnavailable(_outage_message) from error
                sleep(0.5)
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
