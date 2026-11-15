"""Bounded downloads retain a deterministic, chronological frame sequence."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.table import Table
from astropy.time import Time

from sequence import SequenceRequest, build_sequence


class ParallelCutoutTests(unittest.TestCase):
    def test_sampled_archive_result_is_labeled_partial(self):
        observed_mjd = Time("2025-05-03", scale="utc").mjd
        observations = Table(
            [[observed_mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        observations.meta["archive_listing_partial"] = True
        observations.meta["archive_listing_window_note"] = "Only one sampled window answered."
        request = SequenceRequest(
            ra_deg=180, dec_deg=-30, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=20, band="D3",
        )

        def fake_load(*args, **kwargs):
            self.assertEqual(args[2], 20)
            return {
                "image": np.ones((2, 2)), "wcs": object(),
                "target_x": 0.5, "target_y": 0.5,
                "wavelength_um": 2.0, "bandwidth_um": 0.1,
                "brightness_unit": "MJy/sr", "time_mjd": observed_mjd,
                "date_utc": "2025-05-03 00:00:00",
                "cutout_url": "https://example.org/a.fits?cutout",
            }

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.cached_search_observations", return_value=observations),
            patch("sequence.load_cutout", side_effect=fake_load),
            patch("sequence.reproject_interp", side_effect=lambda source, grid, shape_out, **kw:
                  (np.ones(shape_out), np.ones(shape_out))),
        ):
            manifest = build_sequence(request)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(manifest["archive_listing_partial"])
        self.assertEqual(manifest["frame_count"], 1)
        self.assertIn("Only one sampled window answered.", manifest["limitations"])

    def test_empty_incomplete_listing_does_not_claim_no_coverage(self):
        observations = Table(
            names=["t_min", "energy_bandpassname", "access_url"],
            dtype=[float, "U20", "U40"],
        )
        observations.meta["archive_listing_partial"] = True
        request = SequenceRequest(
            ra_deg=180, dec_deg=-30, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=20, band="D3",
        )
        with (
            patch("sequence.cached_search_observations", return_value=observations),
            self.assertRaisesRegex(Exception, "no confirmed images"),
        ):
            build_sequence(request)

    def test_four_parallel_downloads_retain_chronological_output(self):
        first_mjd = Time("2025-05-01", scale="utc").mjd
        observations = Table(
            [
                [first_mjd + index for index in range(8)],
                ["SPHEREx-D4"] * 8,
                [f"https://example.org/{index}.fits" for index in range(8)],
            ],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        request = SequenceRequest(
            ra_deg=120,
            dec_deg=-85,
            start_date="2025-05-01",
            end_date="2025-05-31",
            fov_arcmin=2,
            band="D4",
        )
        barrier = threading.Barrier(4)
        state_lock = threading.Lock()
        concurrent = 0
        peak_concurrent = 0

        def fake_load(observation, coord, size, **kwargs):
            nonlocal concurrent, peak_concurrent
            index = int(str(observation["access_url"]).split("/")[-1].split(".")[0])
            with state_lock:
                concurrent += 1
                peak_concurrent = max(peak_concurrent, concurrent)
            try:
                barrier.wait(timeout=5)
                # Workers finish out of order, independent of input order.
                time.sleep((3 - index % 4) * 0.005)
                return {
                    "image": np.full((2, 2), index + 1, dtype=float),
                    "wcs": object(),
                    "target_x": 0.5,
                    "target_y": 0.5,
                    "wavelength_um": 3.0,
                    "bandwidth_um": 0.2,
                    "brightness_unit": "MJy/sr",
                    "time_mjd": float(observation["t_min"]),
                    "date_utc": Time(float(observation["t_min"]), format="mjd").iso,
                    "cutout_url": f"https://example.org/{index}.fits?cutout",
                }
            finally:
                with state_lock:
                    concurrent -= 1

        def fake_reproject(source, output_wcs, shape_out, **kwargs):
            return np.full(shape_out, source[0][0, 0]), np.ones(shape_out)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.cached_search_observations", return_value=observations),
            patch("sequence.load_cutout", side_effect=fake_load),
            patch("sequence.reproject_interp", side_effect=fake_reproject),
        ):
            manifest = build_sequence(request)
            for frame in manifest["frames"]:
                self.assertTrue((Path(temporary) / manifest["sequence_id"] /
                                 Path(frame["image_url"]).name).is_file())

        self.assertEqual(peak_concurrent, 4)
        self.assertEqual(concurrent, 0)
        self.assertEqual(manifest["attempted_count"], 8)
        self.assertEqual(manifest["frame_count"], 8)
        self.assertEqual(manifest["status"], "ready")
        self.assertEqual(
            [frame["source_url"] for frame in manifest["frames"]],
            [f"https://example.org/{index}.fits" for index in range(8)],
        )
        self.assertEqual([frame["index"] for frame in manifest["frames"]], list(range(8)))


if __name__ == "__main__":
    unittest.main()
