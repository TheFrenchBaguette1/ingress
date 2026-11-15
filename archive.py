import re
from time import monotonic, sleep

from astropy import units as u
from astropy.io import fits
from astropy.time import Time
from astropy.utils.data import conf
from astropy.wcs import WCS
from astroquery.ipac.irsa import IrsaClass
from requests import Session
from requests.exceptions import ConnectionError, Timeout


class ArchiveUnavailable(RuntimeError):
    """IRSA's image search is temporarily unavailable."""


class _TimedSession(Session):
    """Bound PyVO's requests; its default SIA session has no read timeout."""

    deadline = None

    def request(self, *args, **kwargs):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise Timeout("IRSA image-search time budget exhausted")
        kwargs["timeout"] = min(20, remaining)
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
    deadline = monotonic() + 45
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
                raise ArchiveUnavailable(
                    "IRSA's SPHEREx image search is temporarily unavailable. "
                    "Please try again shortly."
                ) from error
            sleep(0.5)


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

    with conf.set_temp("remote_timeout", timeout_seconds):
        with fits.open(cutout_url, cache=cache) as hdul:
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
