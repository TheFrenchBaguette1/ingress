from astropy import units as u
from astropy.io import fits
from astropy.time import Time
from astropy.utils.data import conf
from astropy.wcs import WCS
from astroquery.ipac.irsa import Irsa


def search_observations(coord, search_radius_arcsec, collections):
    """Find observations overlapping the requested coordinates."""
    return Irsa.query_sia(
        pos=(coord, search_radius_arcsec * u.arcsec),
        collection=collections,
    )


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