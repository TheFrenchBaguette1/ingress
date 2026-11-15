"""Measure the native FITS parents of a loaded flipbook in a separate queue."""

import logging
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from astropy.coordinates import SkyCoord
from astropy.time import Time
from pydantic import BaseModel, ConfigDict, Field, model_validator

from flipbook import FOV_ARCMIN
from flipbook_render import archive_product_matches_band
from photometry import PhotometryExcluded, fit_flux, summarize_channels
from photometry_inputs import CALIBRATION_REVISION, PhotometryInputUnavailable, load_photometry_inputs
from sequence import make_output_grid, product_id_for_source


MAX_WORKERS = 2
MAX_ACTIVE_CURVES = 8
MAX_CURVES = 24
MAX_MEASUREMENTS = 4096
EXPOSURE_TIMEOUT_SECONDS = 20
ALGORITHM_VERSION = "fixed-epsf-v1"
MJD_EPOCH = date(1858, 11, 17)
RETRYABLE = frozenset({"archive_unavailable", "calibration_unavailable", "measurement_failed"})

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="spherex-photometry")
_lock = Lock()
_curves = OrderedDict()
_curve_ids = {}
_pending = OrderedDict()
_measurements = OrderedDict()
_inflight = {}
_running = 0


class LightCurveBusy(RuntimeError):
    """There is no room in the light curve queue."""


class LightCurveRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    ra_deg: float | None = Field(default=None, ge=0, lt=360)
    dec_deg: float | None = Field(default=None, ge=-90, le=90)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("The last light curve date must follow the first date.")
        if (self.ra_deg is None) != (self.dec_deg is None):
            raise ValueError("Supply both coordinates for the chosen star.")
        return self


@dataclass(frozen=True)
class _Exposure:
    url: str
    product_id: str
    detector: str
    mjd: float


@dataclass
class _Curve:
    id: str
    key: tuple
    ra: float
    dec: float
    total: int
    processed: int = 0
    running: int = 0
    points: list = field(default_factory=list)
    exclusions: Counter = field(default_factory=Counter)

    @property
    def complete(self):
        return self.processed == self.total


def _collect_exposures(frames, first, last):
    earliest = (first - MJD_EPOCH).days
    end = (last - MJD_EPOCH).days + 1
    exposures = {}
    exclusions = Counter()
    for frame in frames:
        mjd = frame.get("time_mjd")
        if not isinstance(mjd, (int, float)) or not isfinite(mjd):
            exclusions["missing_time"] += 1
            continue
        if not earliest <= mjd < end:
            continue
        url, detector = frame.get("source_url"), frame.get("band")
        if not archive_product_matches_band(url, detector):
            exclusions["unsupported_product"] += 1
            continue
        product = product_id_for_source(url)
        observation = product.split("_spx_", 1)[0]
        previous = exposures.get(observation)
        if previous:
            exclusions["duplicate_observation"] += 1
            if ("/dr1/" not in urlsplit(url).path or "/dr1/" in urlsplit(previous.url).path):
                continue
        exposures[observation] = _Exposure(url, product, detector, mjd)
    return sorted(exposures.values(), key=lambda exposure: (exposure.mjd, exposure.product_id)), exclusions


def _measure(exposure, ra, dec):
    inputs = load_photometry_inputs(exposure.url, ra, dec, timeout_seconds=EXPOSURE_TIMEOUT_SECONDS)
    if f"D{inputs.detector}" != exposure.detector:
        raise PhotometryInputUnavailable("unsupported_product", "The FITS detector does not match the observation.")
    result = fit_flux(inputs.image, inputs.variance, inputs.flags, inputs.psf, inputs.solid_angle_sr,
                      target_xy=inputs.target_xy)
    mjd = float(inputs.mjd) if inputs.mjd is not None else exposure.mjd
    return {
        **result,
        "product_id": exposure.product_id,
        "time_mjd": mjd,
        "time_utc": Time(mjd, format="mjd", scale="utc", precision=6).isot + "Z",
        "time_source": "exposure_midpoint" if inputs.mjd is not None else "archive_start",
        "detector": exposure.detector,
        "channel": int(inputs.channel),
        "wavelength_um": float(inputs.wavelength_um),
        "bandwidth_um": float(inputs.bandwidth_um),
        "channel_wavelength_um": float(inputs.channel_wavelength_um),
        "channel_bandwidth_um": float(inputs.channel_bandwidth_um),
        "calibration_id": inputs.calibration_id,
    }


def _cache_key(curve, exposure):
    return ALGORITHM_VERSION, CALIBRATION_REVISION, exposure.url, curve.ra, curve.dec


def _apply(curve, result):
    point, reason = result
    if point is not None:
        curve.points.append(point)
    else:
        curve.exclusions[reason] += 1
    curve.processed += 1


def _dispatch():
    """Use each open worker slot for one exposure, rotating between curves."""
    global _running
    while _pending and _running < MAX_WORKERS:
        curve_id, exposures = _pending.popitem(last=False)
        curve = _curves[curve_id]
        exposure = exposures.popleft()
        if exposures:
            _pending[curve_id] = exposures
        key = _cache_key(curve, exposure)
        cached = _measurements.get(key)
        if cached is not None:
            _measurements.move_to_end(key)
            _apply(curve, cached)
            continue
        curve.running += 1
        if key in _inflight:
            _inflight[key].append(curve)
            continue
        _running += 1
        _inflight[key] = [curve]
        _executor.submit(_run, curve, exposure)


def _run(curve, exposure):
    global _running
    try:
        result = _measure(exposure, curve.ra, curve.dec), None
    except (PhotometryExcluded, PhotometryInputUnavailable) as error:
        result = None, error.code
        if error.code in RETRYABLE:
            logging.info("Light curve skipped %s (%s): %s", exposure.product_id, error.code,
                         error.__cause__ or error)
    except Exception:
        logging.exception("Light curve measurement failed for %s", exposure.product_id)
        result = None, "measurement_failed"

    with _lock:
        key = _cache_key(curve, exposure)
        if result[1] not in RETRYABLE:
            _measurements[key] = result
            _measurements.move_to_end(key)
            if len(_measurements) > MAX_MEASUREMENTS:
                _measurements.popitem(last=False)
        for waiting_curve in _inflight.pop(key):
            _apply(waiting_curve, result)
            waiting_curve.running -= 1
        _running -= 1
        _dispatch()


def _snapshot(curve):
    return {
        "light_curve_id": curve.id,
        "target": {"ra_deg": curve.ra, "dec_deg": curve.dec},
        "status": "complete" if curve.complete else "running" if curve.running or curve.processed else "queued",
        "total_count": curve.total,
        "processed_count": curve.processed,
        "points": list(curve.points),
        "exclusions": dict(curve.exclusions),
    }


def _with_channels(snapshot):
    snapshot["points"].sort(key=lambda point: (point["time_mjd"], point["product_id"]))
    snapshot["channels"] = summarize_channels(snapshot["points"])
    return snapshot


def start_light_curve(search, frames, request):
    first = request.start_date or search.start_date
    last = request.end_date or search.end_date
    if last < first:
        raise ValueError("The last light curve date must follow the first date.")
    ra, dec = search.ra_deg, search.dec_deg
    if request.ra_deg is not None:
        center = SkyCoord(ra, dec, unit="deg", frame="icrs")
        wcs, (width, height), _ = make_output_grid(center, FOV_ARCMIN)
        x, y = wcs.world_to_pixel_values(request.ra_deg, request.dec_deg)
        if not (-0.500001 <= x <= width - 0.499999 and -0.500001 <= y <= height - 0.499999):
            raise ValueError("The chosen star lies outside the loaded sky patch.")
        ra, dec = request.ra_deg, request.dec_deg
    exposures, exclusions = _collect_exposures(frames, first, last)
    key = ra, dec, tuple(exposures), tuple(sorted(exclusions.items()))

    with _lock:
        curve = _curves.get(_curve_ids.get(key))
        if curve and (not curve.complete or RETRYABLE.isdisjoint(curve.exclusions)):
            _curves.move_to_end(curve.id)
        else:
            if exposures and sum(not entry.complete for entry in _curves.values()) >= MAX_ACTIVE_CURVES:
                raise LightCurveBusy("The light curve queue is full. Try again shortly.")
            if len(_curves) >= MAX_CURVES:
                expired = next(entry for entry in _curves.values() if entry.complete)
                del _curves[expired.id]
                if _curve_ids.get(expired.key) == expired.id:
                    del _curve_ids[expired.key]

            skipped = sum(exclusions.values())
            curve = _Curve(uuid4().hex, key, ra, dec, len(exposures) + skipped,
                           processed=skipped, exclusions=exclusions)
            _curves[curve.id] = curve
            _curve_ids[key] = curve.id
            if exposures:
                _pending[curve.id] = deque(exposures)
                _dispatch()
        snapshot = _snapshot(curve)
    return _with_channels(snapshot)


def get_light_curve(curve_id):
    with _lock:
        curve = _curves.get(curve_id)
        if curve is None:
            return None
        _curves.move_to_end(curve_id)
        snapshot = _snapshot(curve)
    return _with_channels(snapshot)
