"""Native pixel photometry and the wavelength-matched variability test."""

from math import hypot
import unittest

import numpy as np

from photometry import PhotometryExcluded, comparable_passband, fit_flux, summarize_channels


def _star_stamp(flux, target_xy=(5.26, 4.88)):
    y, x = np.indices((11, 11))
    distance = np.hypot(x - target_xy[0], y - target_xy[1])
    psf = np.exp(-0.5 * (distance / 0.85)**2)
    psf /= psf.sum()
    area = 8.9e-10 * (1 + (x - 5) / 100 + (y - 5) / 80)
    background = 0.24
    image = flux * psf / (area * 1e12) + background
    variance = (0.0025 + 0.0001 * x)**2
    flags = np.zeros(image.shape, dtype=np.uint32)
    return image, variance, flags, psf, area


def _point(identifier, date, flux, *, channel=7, wavelength=2.85, bandwidth=0.09, error=2):
    return {
        "product_id": identifier,
        "time_utc": f"2025-06-{date:02d}T12:30:00Z",
        "detector": "D4",
        "channel": channel,
        "wavelength_um": wavelength,
        "bandwidth_um": bandwidth,
        "channel_wavelength_um": 2.85 + (channel - 7) * 0.09,
        "channel_bandwidth_um": 0.09,
        "flux_ujy": flux,
        "stat_error_ujy": error,
    }


class NativePhotometryTests(unittest.TestCase):
    def test_recovers_injected_flux_and_uncertainty_at_different_subpixel_positions(self):
        for flux, target in ((1130, (5.0, 5.0)), (720, (4.56, 5.47)), (-12, (5.32, 4.92))):
            with self.subTest(flux=flux, target=target):
                image, variance, flags, psf, area = _star_stamp(flux, target)
                result = fit_flux(image, variance, flags, psf, area, target_xy=target)
                self.assertAlmostEqual(result["flux_ujy"], flux, places=8)
                design = np.stack((psf, area), axis=-1).reshape(-1, 2)
                noise = (variance * (area * 1e12)**2).ravel()
                expected_stat = np.sqrt(np.linalg.inv(design.T @ (design / noise[:, None]))[0, 0])
                self.assertAlmostEqual(result["stat_error_ujy"], expected_stat)
                self.assertAlmostEqual(result["systematic_error_ujy"], abs(flux) * 0.03)
                self.assertAlmostEqual(result["error_ujy"], hypot(expected_stat, flux * 0.03))

    def test_statistical_error_matches_repeated_injections(self):
        # For a signed low-S/N star, repeated noisy stamps should cover the
        # injected value in approximately 68% of reported statistical intervals.
        target = (5.26, 4.88)
        truth = 1.5
        image, variance, flags, psf, area = _star_stamp(truth, target)
        rng = np.random.default_rng(34952)
        inside = 0
        for _ in range(500):
            observed = image + rng.normal(scale=np.sqrt(variance))
            result = fit_flux(observed, variance, flags, psf, area, target_xy=target)
            inside += abs(result["flux_ujy"] - truth) <= result["stat_error_ujy"]
        self.assertGreater(inside / 500, 0.62)
        self.assertLess(inside / 500, 0.74)

    def test_a_flagged_neighbor_and_missing_wing_do_not_change_the_flux(self):
        target = (5.26, 4.88)
        image, variance, flags, psf, area = _star_stamp(700, target)
        flags[5, 5] = 1 << 21  # Target itself is a known source.
        flags[5, 9] = 1 << 21  # Keep a bright neighbor out of the background fit.
        flags[5, 7] = 1 << 9  # Masked, nonzero PSF wing must stay in normalization.
        image[5, 9] = 500
        image[5, 7] = 500
        result = fit_flux(image, variance, flags, psf, area, target_xy=target)
        self.assertAlmostEqual(result["flux_ujy"], 700)
        self.assertLess(result["psf_coverage"], 1)

    def test_masks_core_and_rejects_saturation(self):
        target = (5.26, 4.88)
        for bit, reason in [(9, "masked_core"), (15, "saturated_core")]:
            with self.subTest(bit=bit):
                image, variance, flags, psf, area = _star_stamp(1200, target)
                flags[5, 5] = 1 << bit
                with self.assertRaises(PhotometryExcluded) as error:
                    fit_flux(image, variance, flags, psf, area, target_xy=target)
                self.assertEqual(error.exception.code, reason)

    def test_rejects_a_large_mask_around_an_intact_core(self):
        target = (5.26, 4.88)
        image, variance, flags, psf, area = _star_stamp(110, target)
        y, x = np.indices(image.shape)
        flags[np.hypot(x - target[0], y - target[1]) > 1] = 1 << 9
        with self.assertRaises(PhotometryExcluded) as error:
            fit_flux(image, variance, flags, psf, area, target_xy=target)
        self.assertEqual(error.exception.code, "insufficient_psf_coverage")

    def test_rejects_a_profile_that_disagrees_with_the_psf(self):
        target = (5.26, 4.88)
        image, variance, flags, psf, area = _star_stamp(700, target)
        image[4, 5] += 0.75
        with self.assertRaises(PhotometryExcluded) as error:
            fit_flux(image, variance, flags, psf, area, target_xy=target)
        self.assertEqual(error.exception.code, "poor_fit")


class VariabilityTests(unittest.TestCase):
    def test_overlap_checks_both_center_and_width(self):
        self.assertTrue(comparable_passband(2.85, 0.09, 2.85, 0.09))
        self.assertTrue(comparable_passband(2.86, 0.09, 2.85, 0.09))
        self.assertFalse(comparable_passband(2.87, 0.09, 2.85, 0.09))
        self.assertFalse(comparable_passband(2.85, 0.15, 2.85, 0.09))
        self.assertFalse(comparable_passband(2.85, None, 2.85, 0.09))

    def test_detects_variable_source_with_three_matched_days(self):
        points = [_point("p1", 1, 100), _point("p2", 8, 150), _point("p3", 19, 200)]
        result, = summarize_channels(points)
        self.assertEqual(result["variability"], "candidate")
        self.assertLess(result["p_value"], 0.01)
        self.assertEqual(result["day_count"], 3)

    def test_wavelength_only_color_changes_and_duplicates_do_not_report_variability(self):
        points = [
            _point("p1", 1, 100), _point("p2", 2, 99), _point("p3", 3, 101),
            _point("shifted", 5, 200, wavelength=2.89),
            _point("new_channel", 7, 300, wavelength=2.94, channel=8),
            _point("p2", 2, 900),
        ]
        first, second = summarize_channels(points)
        self.assertEqual(first["channel"], 7)
        self.assertEqual(first["point_count"], 4)
        self.assertEqual(first["comparable_count"], 3)
        self.assertEqual(first["variability"], "no_significant_change")
        self.assertEqual(second["variability"], "insufficient_observations")

    def test_repeats_on_the_same_day_count_as_a_single_epoch(self):
        points = [_point("p1", 1, 100), _point("p2", 1, 150), _point("p3", 2, 200)]
        result, = summarize_channels(points)
        self.assertEqual(result["day_count"], 2)
        self.assertEqual(result["variability"], "insufficient_observations")
        self.assertIsNone(result["p_value"])

    def test_daily_estimate_applies_calibration_floor_once(self):
        points = [
            _point(f"day-{day}-{exposure}", day, flux, error=0.01)
            for day, flux in [(1, 97), (2, 100), (3, 103)] for exposure in range(20)
        ]
        result, = summarize_channels(points)
        self.assertEqual(result["variability"], "no_significant_change")
        self.assertGreater(result["p_value"], 0.30)
