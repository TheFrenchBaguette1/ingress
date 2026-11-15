"""Forced point-source photometry and same-channel variability tests."""

from collections import defaultdict
from math import hypot

import numpy as np
from scipy.stats import chi2


SCATTER_FRACTION = 0.03
MIN_PSF_COVERAGE = 0.80
MIN_PASSBAND_OVERLAP = 0.80
MIN_OBSERVATION_DAYS = 3
VARIABILITY_P_LIMIT = 0.01

# SPHEREx Explanatory Supplement §3.3 photometry mask and core quality flags.
_PHOTOMETRY_MASK = sum(1 << bit for bit in (2, 6, 9, 10, 11, 15, 17, 26, 27, 28, 29))
_SATURATION_MASK = (1 << 1) | (1 << 15) | (1 << 26)
_SOURCE = 1 << 21
_FIT_QUALITY_RADIUS_PIXELS = 2.5
_CORE_RADIUS_PIXELS = 1.0
_MAX_FIT_QUALITY = 3.0


class PhotometryExcluded(ValueError):
    """A frame that can be viewed but cannot provide trustworthy photometry."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fit_flux(image, variance, flags, psf, solid_angle_sr, *, target_xy):
    """Fit a normalized native ePSF and a constant sky surface brightness.

    Image and variance are in MJy/sr and (MJy/sr)^2. The unit-flux ePSF
    stamp includes pixels outside coverage and must be normalized before
    applying masks. The target is in the stamp's zero-based pixel coordinates.
    """
    image, variance, psf, solid_angle_sr = (
        np.asarray(plane, dtype=np.float64) for plane in (image, variance, psf, solid_angle_sr)
    )
    flags = np.asarray(flags)
    if (image.ndim != 2 or flags.dtype.kind not in "ui" or any(
            plane.shape != image.shape for plane in (variance, flags, psf, solid_angle_sr))):
        raise PhotometryExcluded("invalid_stamp", "The native photometry planes do not match.")
    if not np.isfinite(psf).all() or not np.isclose(psf.sum(), 1.0, atol=1e-6):
        raise PhotometryExcluded("invalid_psf", "The calibrated stellar PSF is unavailable or invalid.")
    flags = flags.astype(np.uint32, copy=False)
    y, x = np.indices(image.shape)
    radius = np.hypot(x - target_xy[0], y - target_xy[1])
    core = radius <= _CORE_RADIUS_PIXELS
    fit_quality_region = radius <= _FIT_QUALITY_RADIUS_PIXELS
    if not core.any():
        raise PhotometryExcluded("masked_core", "This exposure does not cover the stellar core.")
    if np.any(flags[core] & _SATURATION_MASK):
        raise PhotometryExcluded("saturated_core", "The stellar core is saturated or strongly nonlinear.")

    valid = (np.isfinite(image) & np.isfinite(variance) & (variance > 0) &
             np.isfinite(solid_angle_sr) & (solid_angle_sr > 0) &
             ((flags & _PHOTOMETRY_MASK) == 0))
    if not valid[core].all():
        raise PhotometryExcluded("masked_core", "The stellar core includes missing or flagged pixels.")
    # A target can itself carry SOURCE. Exclude SOURCE only in the background
    # region, where the flag marks potential contamination by other sources.
    valid &= fit_quality_region | ((flags & _SOURCE) == 0)
    positive_psf = np.maximum(psf, 0)
    coverage = float(positive_psf[valid].sum() / positive_psf.sum())
    if coverage < MIN_PSF_COVERAGE or np.count_nonzero(valid) < 3:
        raise PhotometryExcluded("insufficient_psf_coverage", "Too little of the stellar PSF is usable.")

    conversion = solid_angle_sr[valid] * 1e12
    values = image[valid] * conversion
    noise = np.sqrt(variance[valid]) * conversion
    template = psf[valid]
    # Scale the background column for numerical conditioning. It remains a
    # constant surface brightness, integrated over each native pixel's area.
    background = conversion / np.median(conversion)
    design = np.column_stack((template, background)) / noise[:, None]
    observations = values / noise
    parameters, _, rank, _ = np.linalg.lstsq(design, observations, rcond=None)
    if rank != 2:
        raise PhotometryExcluded("invalid_fit", "The star and background could not be separated.")

    # Project out the background column. The inverse squared norm of this
    # projected template is the flux variance from the fitted covariance.
    star_column, sky_column = design.T
    projected = star_column - sky_column * (star_column @ sky_column) / (sky_column @ sky_column)
    uncertainty = float(1 / np.linalg.norm(projected))
    flux = float(parameters[0])
    residual = np.abs(observations - design @ parameters)
    fit_quality = float(residual[fit_quality_region[valid]].mean())
    if not np.isfinite([uncertainty, flux, fit_quality]).all():
        raise PhotometryExcluded("invalid_fit", "The stellar fit returned invalid photometry.")
    if fit_quality > _MAX_FIT_QUALITY:
        raise PhotometryExcluded("poor_fit", "The flux profile disagrees with the stellar PSF.")

    systematic = SCATTER_FRACTION * abs(flux)
    return {
        "flux_ujy": flux,
        "stat_error_ujy": uncertainty,
        "systematic_error_ujy": systematic,
        "error_ujy": hypot(uncertainty, systematic),
        "psf_coverage": coverage,
        "fit_quality": fit_quality,
    }


def comparable_passband(wavelength, bandwidth, channel_wavelength, channel_bandwidth):
    """Require at least 80% intersection over union of the two passbands."""
    values = (wavelength, bandwidth, channel_wavelength, channel_bandwidth)
    if any(value is None or not np.isfinite(value) or value <= 0 for value in values):
        return False
    left = max(wavelength - bandwidth / 2, channel_wavelength - channel_bandwidth / 2)
    right = min(wavelength + bandwidth / 2, channel_wavelength + channel_bandwidth / 2)
    overlap = max(0.0, right - left)
    return bool(overlap >= (MIN_PASSBAND_OVERLAP - 1e-12) * (bandwidth + channel_bandwidth - overlap))


def _constant_flux_test(daily_groups):
    daily_flux = []
    daily_errors = []
    for points in daily_groups.values():
        fluxes = np.array([point["flux_ujy"] for point in points])
        errors = np.array([point["stat_error_ujy"] for point in points])
        weights = 1 / errors**2
        day_flux = float(np.average(fluxes, weights=weights))
        day_stat_error = float(np.sqrt(1 / weights.sum()))
        daily_flux.append(day_flux)
        daily_errors.append(hypot(day_stat_error, SCATTER_FRACTION * day_flux))

    mean = float(np.average(daily_flux, weights=1 / np.square(daily_errors))) if daily_flux else None
    if len(daily_flux) < MIN_OBSERVATION_DAYS:
        return "insufficient_observations", None, mean
    statistic = float(np.sum(np.square((np.array(daily_flux) - mean) / daily_errors)))
    probability = float(chi2.sf(statistic, len(daily_flux) - 1))
    result = "candidate" if probability < VARIABILITY_P_LIMIT else "no_significant_change"
    return result, probability, mean


def summarize_channels(points):
    """Summarize distinct products by channel and test independent UTC days.

    The caller has already validated the product identities and timestamps.
    Every successful exposure can be charted. Only passband-matched points
    contribute to the constant-flux test.
    """
    channels = defaultdict(list)
    seen = set()
    for point in points:
        product_id = point["product_id"]
        if product_id not in seen:
            seen.add(product_id)
            channels[point["detector"], point["channel"]].append(point)

    summaries = []
    for (detector, channel), exposures in channels.items():
        center = exposures[0]["channel_wavelength_um"]
        width = exposures[0]["channel_bandwidth_um"]
        daily_groups = defaultdict(list)
        for point in exposures:
            if comparable_passband(point["wavelength_um"], point["bandwidth_um"], center, width):
                daily_groups[point["time_utc"][:10]].append(point)
        result, probability, mean = _constant_flux_test(daily_groups)
        summaries.append({
            "detector": detector,
            "channel": channel,
            "wavelength_um": center,
            "bandwidth_um": width,
            "point_count": len(exposures),
            "comparable_count": sum(map(len, daily_groups.values())),
            "day_count": len(daily_groups),
            "variability": result,
            "p_value": probability,
            "mean_flux_ujy": mean,
        })
    return sorted(summaries, key=lambda entry: (
        -entry["day_count"], -entry["comparable_count"], -entry["point_count"],
        entry["wavelength_um"], entry["detector"], entry["channel"],
    ))
