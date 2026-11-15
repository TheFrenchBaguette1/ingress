"""Native DR1/QR3 inputs for forced stellar photometry."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from math import floor, isfinite
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import NoConvergence, WCS
import numpy as np
from requests import RequestException
from scipy.interpolate import RectBivariateSpline

from flipbook_render import DETECTOR_IN_PRODUCT, archive_product_matches_band
from henry_fits import FITS_BLOCK, MAX_RANGE, ParentReadUnavailable, _RangeReader, _flag_rows, _header, _session


_SPECTRAL_VERSION = "cal-swcs-v5-2026-191"
_SOLID_ANGLE_VERSION = "cal-sapm-v3-2026-191"
_CHANNEL_VERSION = "cal-sch-v2-2026-229"
CALIBRATION_REVISION = "/".join((_SPECTRAL_VERSION, _SOLID_ANGLE_VERSION, _CHANNEL_VERSION))
_DETECTOR_SIZE = 2040
_HALF_STAMP = 5
_STAMP_SIZE = 2 * _HALF_STAMP + 1
_MISSING = 1 << 9
_SURFACE_BRIGHTNESS = u.MJy / u.sr
_FIT_IO_ERRORS = (OSError, ValueError, KeyError, TypeError, RequestException)
_CALIBRATION_IO_ERRORS = (*_FIT_IO_ERRORS, StopIteration)
_CACHE_LOCK = Lock()
_PSF_TABLES = {}
_SPECTRAL_GRIDS = {}
_CHANNEL_TABLES = {}
# SPHEREx Explanatory Supplement v2.1, Tables 29–34. These provide the
# nominal center and resolving power if a mirror omits the metadata table.
_NOMINAL_CHANNELS = (
    ((0.753, 0.771, 0.790, 0.809, 0.828, 0.848, 0.868, 0.889, 0.910,
      0.932, 0.955, 0.978, 1.001, 1.025, 1.050, 1.075, 1.102),
     (42, 42, 41, 40, 40, 40, 40, 39, 39, 39, 39, 38, 38, 38, 38, 37, 37)),
    ((1.113, 1.139, 1.167, 1.195, 1.224, 1.254, 1.284, 1.315, 1.346,
      1.378, 1.412, 1.446, 1.481, 1.517, 1.554, 1.592, 1.631),
     (43, 42, 42, 41, 41, 41, 42, 42, 42, 42, 41, 41, 41, 41, 41, 41, 41)),
    ((1.656, 1.695, 1.735, 1.775, 1.818, 1.861, 1.905, 1.949, 1.995,
      2.041, 2.089, 2.139, 2.189, 2.240, 2.292, 2.345, 2.399),
     (42, 42, 41, 41, 41, 41, 41, 41, 41, 40, 40, 40, 40, 40, 40, 40, 41)),
    ((2.445, 2.506, 2.575, 2.646, 2.719, 2.794, 2.871, 2.951, 3.033,
      3.118, 3.204, 3.293, 3.383, 3.476, 3.570, 3.667, 3.768),
     (42, 34, 35, 35, 35, 35, 34, 34, 34, 34, 35, 35, 35, 36, 36, 36, 36)),
    ((3.834, 3.867, 3.901, 3.936, 3.970, 4.005, 4.041, 4.076, 4.112,
      4.148, 4.184, 4.221, 4.257, 4.294, 4.331, 4.368, 4.405),
     (109, 109, 109, 109, 109, 110, 110, 111, 112, 113, 113, 115, 114, 115, 116, 116, 117)),
    ((4.435, 4.468, 4.500, 4.533, 4.567, 4.601, 4.635, 4.669, 4.704,
      4.739, 4.774, 4.808, 4.843, 4.879, 4.915, 4.952, 4.989),
     (129, 125, 127, 126, 127, 126, 127, 126, 128, 130, 131, 132, 131, 130, 131, 132, 135)),
)


class PhotometryInputUnavailable(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PhotometryInputs:
    image: np.ndarray
    variance: np.ndarray
    flags: np.ndarray
    solid_angle_sr: np.ndarray
    psf: np.ndarray
    target_xy: tuple[float, float]
    detector: int
    channel: int
    wavelength_um: float
    bandwidth_um: float
    channel_wavelength_um: float
    channel_bandwidth_um: float
    mjd: float | None
    calibration_id: str


@dataclass(frozen=True)
class _NativeStamp:
    image: np.ndarray
    variance: np.ndarray
    flags: np.ndarray
    native_xy: tuple[float, float]
    native_origin: tuple[int, int]
    psf_pixels: np.ndarray
    psf_oversampling: tuple[int, int]
    wave_grids: tuple[str, ...]
    mjd: float | None


def _extension_layouts(reader, names=None):
    remaining = set(names) if names is not None else None
    start, prefix = 0, b""
    while reader.size is None or start < reader.size:
        layout = _header(reader, start, prefix, first_read=FITS_BLOCK)
        header, data, size, prefix = layout
        name = str(header.get("EXTNAME", "")).strip()
        if remaining is None:
            yield name, layout
        elif name in remaining:
            yield name, layout
            remaining.remove(name)
            if not remaining:
                return
        start = data + size
        prefix = prefix[size:]


def _fits_part(reader, layout):
    header, offset, padded_size, prefix = layout
    if not 0 < padded_size <= MAX_RANGE:
        raise ParentReadUnavailable("FITS table exceeds the supported read size.")
    payload = prefix[:padded_size] if len(prefix) >= padded_size else reader.read(offset, offset + padded_size)
    primary = fits.PrimaryHDU().header.tostring().encode("ascii")
    return fits.open(BytesIO(primary + header.tostring().encode("ascii") + payload), memmap=False)


def _array_shape(header):
    return header.get("NAXIS2"), header.get("NAXIS1")


def _image_slice(reader, layout, bounds, shape):
    header, offset, _, _ = layout
    dtype = {-64: ">f8", -32: ">f4", 32: ">i4", 16: ">i2", 8: "u1"}.get(header.get("BITPIX"))
    if not (dtype and header.get("NAXIS") == 2 and _array_shape(header) == shape
            and header.get("XTENSION", "IMAGE").strip() == "IMAGE" and not header.get("PCOUNT", 0)):
        raise ParentReadUnavailable("A native FITS image plane has an unexpected layout.")
    x0, x1, y0, y1 = bounds
    height, width = shape
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ParentReadUnavailable("The stamp is outside its native FITS image.")
    row_bytes = width * np.dtype(dtype).itemsize
    data = reader.read(offset + y0 * row_bytes, offset + y1 * row_bytes)
    raw = np.frombuffer(data, dtype=dtype).reshape(y1 - y0, width)[:, x0:x1]
    if raw.dtype.kind == "f" or header.get("BZERO", 0) or header.get("BSCALE", 1) != 1:
        return raw.astype(float) * header.get("BSCALE", 1) + header.get("BZERO", 0)
    return raw.copy()


def _target_pixel(header, coord):
    height, width = _array_shape(header)
    try:
        x, y = map(float, WCS(header).celestial.world_to_pixel(coord))
    except NoConvergence as error:
        raise PhotometryInputUnavailable("outside_detector", "The crosshair could not be located in this exposure.") from error
    if not (isfinite(x) and isfinite(y) and -0.5 <= x < width - 0.5 and -0.5 <= y < height - 0.5):
        raise PhotometryInputUnavailable("outside_detector", "The crosshair is outside this exposure's detector.")
    return x, y


def _stamp_origin(x, y):
    return floor(x + 0.5) - _HALF_STAMP, floor(y + 0.5) - _HALF_STAMP


def _clipped_bounds(origin, shape):
    x0, y0 = origin
    height, width = shape
    return max(0, x0), min(width, x0 + _STAMP_SIZE), max(0, y0), min(height, y0 + _STAMP_SIZE)


def _pad_stamp(array, bounds, origin, fill, dtype):
    x0, x1, y0, y1 = bounds
    native_x, native_y = origin
    stamp = np.full((_STAMP_SIZE, _STAMP_SIZE), fill, dtype=dtype)
    stamp[y0 - native_y:y1 - native_y, x0 - native_x:x1 - native_x] = array
    return stamp


def _unit_scale(header, unit, *, default=None):
    return u.Unit(header.get("BUNIT", default)).to(unit)


def _image_stamp(image, variance, flags, image_header, variance_header, bounds, origin):
    if image.shape != flags.shape or image.shape != variance.shape or flags.dtype.kind not in "iu":
        raise ParentReadUnavailable("The native image, variance, and flags do not match.")
    image_scale = _unit_scale(image_header, _SURFACE_BRIGHTNESS)
    variance_scale = _unit_scale(variance_header, _SURFACE_BRIGHTNESS ** 2)
    return (
        _pad_stamp(image * image_scale, bounds, origin, np.nan, np.float64),
        _pad_stamp(variance * variance_scale, bounds, origin, np.nan, np.float64),
        _pad_stamp(flags, bounds, origin, _MISSING, np.uint32),
    )


def _wave_digest(table, offset=(0, 0)):
    if len(table) != 1:
        raise ParentReadUnavailable("The exposure's wavelength lookup has an unsupported shape.")
    digest = sha256()
    for column, shift in zip(("X", "Y", "VALUES"), (*offset, 0), strict=True):
        values = np.asarray(table[column][0], dtype=float)
        if not values.size or not np.isfinite(values).all():
            raise ParentReadUnavailable("The exposure's wavelength calibration is invalid.")
        digest.update((values + shift).astype(">f8").tobytes())
    return digest.hexdigest()


def _mjd_midpoint(header):
    avg, first, last = (header.get(name) for name in ("MJD-AVG", "MJD-BEG", "MJD-END"))
    if isinstance(avg, (int, float)) and isfinite(avg):
        return float(avg)
    if (isinstance(first, (int, float)) and isinstance(last, (int, float))
            and isfinite(first) and isfinite(last) and first <= last):
        return (first + last) / 2
    return None


def _epsf_table(hdu):
    oversampling = (hdu.header.get("OVSMPY"), hdu.header.get("OVSMPX"))
    if not all(type(value) is int and 1 <= value <= 20 for value in oversampling):
        raise ParentReadUnavailable("The exposure has no supported effective PSF sampling.")
    table = hdu.data
    centers = np.column_stack((table["XCENTER"], table["YCENTER"]))
    pixels = np.array(table["EPSF"], dtype=float, copy=True)
    if (pixels.ndim != 3 or not 4 <= min(pixels.shape[1:]) <= max(pixels.shape[1:]) <= 101
            or not 1 <= len(centers) == len(pixels) <= 1000 or not np.isfinite(centers).all()):
        raise ParentReadUnavailable("The exposure's effective PSF grid is invalid.")
    return centers, pixels, oversampling


def _epsf_at(table, xy):
    centers, pixels, oversampling = table
    zone = int(np.argmin(np.sum((centers - xy) ** 2, axis=1)))
    psf = pixels[zone]
    if not np.isfinite(psf).all():
        raise ParentReadUnavailable("The effective PSF at this position is invalid.")
    return psf, oversampling


def _read_s3_stamp(source_url, coord, deadline):
    reader = _RangeReader(source_url, deadline)
    layouts = _extension_layouts(reader, ("IMAGE", "FLAGS", "VARIANCE", "EPSF", "WCS-WAVE"))
    name, image_layout = next(layouts, (None, None))
    if name != "IMAGE":
        raise ParentReadUnavailable("The native spectral IMAGE plane was not found.")
    header = image_layout[0]
    shape = _array_shape(header)
    if shape != (_DETECTOR_SIZE, _DETECTOR_SIZE):
        raise ParentReadUnavailable("The spectral IMAGE uses an unsupported detector size.")
    xy = _target_pixel(header, coord)
    origin = _stamp_origin(*xy)
    bounds = _clipped_bounds(origin, shape)
    x0, x1, y0, y1 = bounds
    other = dict(layouts)
    if len(other) != 4:
        raise ParentReadUnavailable("The exposure does not contain all native photometry extensions.")
    science = _image_slice(reader, image_layout, bounds, shape)
    var_layout = other["VARIANCE"]
    variance = _image_slice(reader, var_layout, bounds, shape)
    mask_layout = other["FLAGS"]
    if mask_layout[0].get("ZIMAGE"):
        flags = _flag_rows(reader, None, shape[1], shape[0], y0, y1, layout=mask_layout)[:, x0:x1]
    else:
        flags = _image_slice(reader, mask_layout, bounds, shape)
    epsf_layout = other["EPSF"]
    psf_header = epsf_layout[0]
    # FITS data checksum makes the reused table specific to its parent extension.
    psf_checksum = psf_header.get("DATASUM")
    psf_key = (psf_header.get("ARRAY"), psf_checksum, psf_header.get("NAXIS2"),
               psf_header.get("OVSMPY"), psf_header.get("OVSMPX"))
    with _CACHE_LOCK:
        psf_table = _PSF_TABLES.get(psf_key) if psf_checksum is not None else None
    if psf_table is None:
        with _fits_part(reader, epsf_layout) as hdus:
            psf_table = _epsf_table(hdus[1])
        if psf_checksum is not None:
            with _CACHE_LOCK:
                if len(_PSF_TABLES) < 12:
                    _PSF_TABLES.setdefault(psf_key, psf_table)
    psf, oversampling = _epsf_at(psf_table, xy)
    with _fits_part(reader, other["WCS-WAVE"]) as hdus:
        wave_grid = _wave_digest(hdus[1].data)
    image, variance, flags = _image_stamp(science, variance, flags, header, var_layout[0], bounds, origin)
    return _NativeStamp(image, variance, flags, xy, origin, psf, oversampling, (wave_grid,), _mjd_midpoint(header))


def _read_cutout_stamp(source_url, coord, deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise TimeoutError("The cutout budget was exhausted.")
    url = f"{source_url}?center={coord.ra.deg},{coord.dec.deg}d&size={2 / 60}"
    with _session().get(url, headers={"Accept-Encoding": "identity"}, allow_redirects=False,
                        stream=True, timeout=(min(4, left / 2), min(8, left / 2))) as response:
        if response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ParentReadUnavailable(f"The same-parent IRSA cutout returned HTTP {response.status_code}.")
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if len(payload) + len(chunk) > MAX_RANGE or monotonic() >= deadline:
                raise ParentReadUnavailable("The same-parent IRSA cutout exceeded its size or time budget.")
            payload.extend(chunk)
    with fits.open(BytesIO(payload), memmap=False) as hdus:
        header = hdus["IMAGE"].header
        xy = _target_pixel(header, coord)
        offset = (1 - float(header["CRPIX1A"]), 1 - float(header["CRPIX2A"]))
        if not all(isfinite(value) and value.is_integer() for value in offset):
            raise ParentReadUnavailable("The same-parent cutout lacks an exact native-pixel offset.")
        offset = tuple(map(int, offset))
        native_xy = tuple(value + shift for value, shift in zip(xy, offset, strict=True))
        native_origin = _stamp_origin(*native_xy)
        origin = tuple(value - shift for value, shift in zip(native_origin, offset, strict=True))
        shape = _array_shape(header)
        if max(shape) > 64:
            raise ParentReadUnavailable("The same-parent IRSA cutout is larger than the supported native stamp.")
        bounds = _clipped_bounds(origin, shape)
        x0, x1, y0, y1 = bounds
        region = np.s_[y0:y1, x0:x1]
        variance_hdu = hdus["VARIANCE"]
        image, variance, flags = _image_stamp(hdus["IMAGE"].data[region], variance_hdu.data[region],
                                              hdus["FLAGS"].data[region], header, variance_hdu.header, bounds, origin)
        psf, oversampling = _epsf_at(_epsf_table(hdus["EPSF"]), native_xy)
        # Depending on the cutout representation, shifts can be expressed by
        # WCS reference pixels or by translating the lookup table axes.
        wave_table = hdus["WCS-WAVE"].data
        original = _wave_digest(wave_table)
        translated = _wave_digest(wave_table, offset)
    return _NativeStamp(image, variance, flags, native_xy, native_origin, psf, oversampling,
                        (original, translated), _mjd_midpoint(header))


def _psf_stamp(native):
    tx, ty = (pixel - origin for pixel, origin in zip(native.native_xy, native.native_origin, strict=True))
    sy, sx = native.psf_oversampling
    height, width = native.psf_pixels.shape
    kernel_x = (np.arange(_STAMP_SIZE) - tx) * sx + (width - 1) / 2
    kernel_y = (np.arange(_STAMP_SIZE) - ty) * sy + (height - 1) / 2
    psf = RectBivariateSpline(np.arange(height), np.arange(width), native.psf_pixels)(kernel_y, kernel_x)
    psf[(kernel_y < 0) | (kernel_y > height - 1), :] = 0
    psf[:, (kernel_x < 0) | (kernel_x > width - 1)] = 0
    total = psf.sum()
    if not np.isfinite(total) or total <= 0:
        raise ParentReadUnavailable("The effective PSF cannot be normalized.")
    return psf / total, (float(tx), float(ty))


def _channel_calibration(detector, native, deadline):
    path = f"dr1/spectral_channels/{_CHANNEL_VERSION}/spectral_channels_spx_{_CHANNEL_VERSION}.fits"
    reader = _RangeReader(path, deadline, calibration=True)
    layouts = list(_extension_layouts(reader))
    shape = (_DETECTOR_SIZE, _DETECTOR_SIZE)
    maps = [layout for _, layout in layouts if _array_shape(layout[0]) == shape
            and layout[0].get("NAXIS") == 2 and layout[0].get("XTENSION", "IMAGE").strip() == "IMAGE"]
    if len(maps) != 1:
        raise ParentReadUnavailable("The official spectral channel map was not uniquely identified.")
    nx, ny = (floor(pixel + 0.5) for pixel in native.native_xy)
    channel_value, = _image_slice(reader, maps[0], (nx, nx + 1, ny, ny + 1), shape).ravel()
    channel = int(channel_value)
    if channel != channel_value or not 1 <= channel <= 17:
        raise PhotometryInputUnavailable("outside_channel", "The crosshair falls outside the standard spectral channels in this exposure.")
    with _CACHE_LOCK:
        table = _CHANNEL_TABLES.get(path)
    if table is None:
        table = {}
        for _, layout in layouts:
            header = layout[0]
            columns = {header.get(f"TTYPE{i}") for i in range(1, header.get("TFIELDS", 0) + 1)}
            if {"DETECTOR", "SUBCHAN", "WAVELENGTH", "BANDWIDTH"} <= columns:
                with _fits_part(reader, layout) as hdus:
                    table = {(int(row["DETECTOR"]), int(row["SUBCHAN"])):
                             (float(row["WAVELENGTH"]), float(row["BANDWIDTH"])) for row in hdus[1].data}
                break
        with _CACHE_LOCK:
            _CHANNEL_TABLES.setdefault(path, table)
    centers, resolving_powers = _NOMINAL_CHANNELS[detector - 1]
    center = centers[channel - 1]
    nominal_wave, nominal_width = table.get((detector, channel), (center, center / resolving_powers[channel - 1]))
    return channel, nominal_wave, nominal_width


def _spectral_calibration(detector, native, deadline):
    path = f"dr1/spectral_wcs/{_SPECTRAL_VERSION}/{detector}/spectral_wcs_D{detector}_spx_{_SPECTRAL_VERSION}.fits"
    reader = _RangeReader(path, deadline, calibration=True)
    with _CACHE_LOCK:
        grid = _SPECTRAL_GRIDS.get(path)
    names = ("CWAVE", "CBAND", "WCS-WAVE") if grid is None else ("CWAVE", "CBAND")
    layouts = dict(_extension_layouts(reader, names))
    if grid is None:
        with _fits_part(reader, layouts["WCS-WAVE"]) as hdus:
            grid = _wave_digest(hdus[1].data)
        with _CACHE_LOCK:
            _SPECTRAL_GRIDS.setdefault(path, grid)
    if grid not in native.wave_grids:
        raise ParentReadUnavailable("The exposure and archive spectral calibration versions differ.")
    x, y = np.clip(native.native_xy, 0, _DETECTOR_SIZE - 1)
    x0, y0 = min(floor(x), _DETECTOR_SIZE - 2), min(floor(y), _DETECTOR_SIZE - 2)
    dx, dy = x - x0, y - y0
    weights = np.array([[(1 - dx) * (1 - dy), dx * (1 - dy)], [(1 - dx) * dy, dx * dy]])
    bounds = x0, x0 + 2, y0, y0 + 2
    return tuple(
        float(np.sum(_image_slice(reader, layouts[name], bounds, (_DETECTOR_SIZE, _DETECTOR_SIZE)) * weights))
        * _unit_scale(layouts[name][0], u.um)
        for name in ("CWAVE", "CBAND")
    )


def _solid_angle_calibration(detector, native, deadline):
    group = f"dr1/solid_angle_pixel_map/{_SOLID_ANGLE_VERSION}/{detector}"
    path = f"{group}/solid_angle_pixel_map_D{detector}_spx_{_SOLID_ANGLE_VERSION}.fits"
    reader = _RangeReader(path, deadline, calibration=True)
    _, layout = next(_extension_layouts(reader, ("IMAGE",)))
    shape = (_DETECTOR_SIZE, _DETECTOR_SIZE)
    bounds = _clipped_bounds(native.native_origin, shape)
    image = _image_slice(reader, layout, bounds, shape)
    scale = _unit_scale(layout[0], u.sr, default="arcsec2")
    if not np.isfinite(image).all() or (image <= 0).any():
        raise ParentReadUnavailable("The official pixel solid-angle map contains invalid pixels.")
    return _pad_stamp(image * scale, bounds, native.native_origin, np.nan, np.float64)


def load_photometry_inputs(source_url, ra_deg, dec_deg, *, timeout_seconds=25):
    """Load a calibrated, unchanged native stamp for one trusted parent exposure."""
    path = urlsplit(source_url).path if isinstance(source_url, str) else ""
    product = DETECTOR_IN_PRODUCT.fullmatch(path.rsplit("/", 1)[-1])
    if not product or not archive_product_matches_band(source_url, product["band"]):
        raise PhotometryInputUnavailable("unsupported_product", "The exposure is not a supported DR1 or QR3 parent.")
    detector = int(product["band"][1:])
    coord = SkyCoord(ra_deg, dec_deg, unit="deg", frame="icrs")
    started = monotonic()
    deadline = started + timeout_seconds
    try:
        try:
            native = _read_s3_stamp(source_url, coord, started + timeout_seconds * 0.55)
        except PhotometryInputUnavailable:
            raise
        except _FIT_IO_ERRORS:
            native = _read_cutout_stamp(source_url, coord, started + timeout_seconds * 0.8)
        psf, target_xy = _psf_stamp(native)
    except PhotometryInputUnavailable:
        raise
    except _FIT_IO_ERRORS as error:
        raise PhotometryInputUnavailable("archive_unavailable", "The original exposure's native photometry data could not be read.") from error

    try:
        channel, nominal_wave, nominal_width = _channel_calibration(detector, native, deadline)
        wave, width = _spectral_calibration(detector, native, deadline)
        solid_angle = _solid_angle_calibration(detector, native, deadline)
        if not all(isfinite(value) and value > 0 for value in (wave, width, nominal_wave, nominal_width)):
            raise ParentReadUnavailable("The official wavelength or bandpass calibration is invalid at the crosshair.")
    except PhotometryInputUnavailable:
        raise
    except _CALIBRATION_IO_ERRORS as error:
        raise PhotometryInputUnavailable("calibration_unavailable", "The matching detector calibration could not be read.") from error

    return PhotometryInputs(native.image, native.variance, native.flags, solid_angle, psf, target_xy,
                            detector, channel, wave, width, nominal_wave, nominal_width,
                            native.mjd, CALIBRATION_REVISION)
