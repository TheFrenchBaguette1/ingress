"""A zoom retains parent exposure IDs, aligned science pixels, masks and WCS."""

import tempfile
import unittest
import json
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from astropy.table import Table
from astropy.time import Time
from astropy.io import fits
from astropy.wcs import WCS

from sequence import SequenceRequest, build_sequence
from zoom import ZoomRequest, build_zoom


class TrueZoomTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        first_mjd = Time("2025-05-03", scale="utc").mjd
        self.sources = [
            f"https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W17_4B/{i}.fits"
            for i in (1, 2)
        ]
        self.observations = Table(
            [[first_mjd, first_mjd + 6], ["SPHEREx-D4"] * 2, self.sources],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        self.request = SequenceRequest(
            ra_deg=12, dec_deg=75, start_date="2025-05-01",
            end_date="2025-06-01", fov_arcmin=5, band="D4",
        )
        self.calls = []

    def _load(self, observation, coord, size, **kwargs):
        source = str(observation["access_url"])
        self.calls.append((source, size))
        return {
            "image": np.full((80, 80), 1 if source == self.sources[0] else 2, dtype=float),
            "wcs": object(), "target_x": 39.5, "target_y": 39.5,
            "wavelength_um": 3.05, "bandwidth_um": 0.2,
            "brightness_unit": "MJy/sr", "time_mjd": float(observation["t_min"]),
            "date_utc": Time(float(observation["t_min"]), format="mjd").iso,
            "detector": "SPHEREx-D4", "source_url": source,
            "cutout_url": source + f"?size={size}",
        }

    @staticmethod
    def _reproject(source, output_wcs, shape_out, **kwargs):
        # Simulate changed interpolation in a wider cutout. The existing
        # center must still be bit-for-bit identical after an expansion.
        size = shape_out[0]
        yy, xx = np.indices(shape_out)
        image = (source[0][0, 0] * 1000 + xx + yy * .013
                 + (0.25 if size > 50 else 0)).astype(np.float64)
        footprint = np.ones(shape_out)
        center = size // 2
        footprint[center - 10, center - 7] = 0
        footprint[center + 10, center + 5] = 0
        return image, footprint

    def _science(self, manifest, index):
        image_url = manifest["frames"][index]["image_url"]
        folder = self.root / manifest["sequence_id"]
        if "/zoom_" in image_url:
            folder = folder / image_url.split("/")[-2]
        return np.load(folder / manifest["frames"][index]["science_file"])

    def test_central_five_arcminutes_are_identical_at_all_zoom_levels(self):
        with (
            patch("config.OUTPUT_ROOT", self.root),
            patch("sequence.cached_search_observations", return_value=self.observations) as sia,
            patch("sequence.load_cutout", side_effect=self._load),
            patch("zoom.load_cutout", side_effect=self._load),
            patch("sequence.reproject_interp", side_effect=self._reproject),
            patch("zoom.reproject_interp", side_effect=self._reproject),
        ):
            five = build_sequence(self.request)
            ten = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=10))
            twenty = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=20))
            # Zooming back in must crop widest saved master without fetching.
            calls_before = len(self.calls)
            again = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=5))
            self.assertEqual(len(self.calls), calls_before)

        sia.assert_called_once()
        self.assertEqual([size for _, size in self.calls], [5, 5, 10, 10, 20, 20])
        for old, new in ((five, ten), (ten, twenty), (five, twenty)):
            self.assertEqual(old["sequence_id"], new["sequence_id"])
            self.assertEqual(old["frame_count"], new["frame_count"])
            self.assertEqual(old["display"], new["display"])
            self.assertEqual(
                [(f["source_url"], f["date_utc"], f["detector"], f["band"], f["center_wavelength_um"])
                 for f in old["frames"]],
                [(f["source_url"], f["date_utc"], f["detector"], f["band"], f["center_wavelength_um"])
                 for f in new["frames"]],
            )

        self.assertEqual([five["grid"]["width_pixels"], ten["grid"]["width_pixels"],
                          twenty["grid"]["width_pixels"]], [50, 100, 200])
        wcs = [WCS(fits.Header.fromstring(manifest["grid"]["wcs_header"], sep="\n"))
               for manifest in (five, ten, twenty)]
        original_sky = wcs[0].pixel_to_world_values(11, 19)
        for larger_wcs, offset in ((wcs[1], 25), (wcs[2], 75)):
            np.testing.assert_allclose(larger_wcs.pixel_to_world_values(11 + offset, 19 + offset),
                                       original_sky, rtol=0, atol=1e-10)
        for index in range(2):
            original = self._science(five, index)
            at_ten = self._science(ten, index)[25:75, 25:75]
            at_twenty = self._science(twenty, index)[75:125, 75:125]
            np.testing.assert_array_equal(np.isnan(original), np.isnan(at_ten))
            np.testing.assert_array_equal(np.isnan(original), np.isnan(at_twenty))
            np.testing.assert_array_equal(original, at_ten)
            np.testing.assert_array_equal(original, at_twenty)
            # Transparent PNG pixels must be the exact same no-data mask,
            # with sky image y axis reversed for the top-left PNG origin.
            for manifest in (five, ten, twenty):
                folder = self.root / manifest["sequence_id"]
                if manifest is not five:
                    folder /= manifest["frames"][index]["image_url"].split("/")[-2]
                with Image.open(folder / f"frame_{index:03d}.png") as preview:
                    alpha = np.asarray(preview)[::-1, :, 3] == 0
                np.testing.assert_array_equal(alpha, np.isnan(self._science(manifest, index)))
        self.assertEqual(five["frames"], again["frames"])
        self.assertEqual(twenty["frames"][0]["zoom_outer_region_status"], "loaded")
        self.assertEqual(twenty["frames"][0]["product_id"], "1.fits")
        self.assertEqual(twenty["frames"][0]["pixel_dimensions"], {"width": 200, "height": 200})
        self.assertEqual(twenty["frames"][0]["zoom_native_cutout_dimensions"],
                         {"width": 80, "height": 80})
        self.assertEqual(len(twenty["grid"]["sky_footprint"]), 4)

    def test_unavailable_wider_cutout_keeps_selected_center_and_discloses_masked_outskirts(self):
        with (
            patch("config.OUTPUT_ROOT", self.root),
            patch("sequence.cached_search_observations", return_value=self.observations),
            patch("sequence.load_cutout", side_effect=self._load),
            patch("sequence.reproject_interp", side_effect=self._reproject),
        ):
            five = build_sequence(self.request)
            with patch("zoom.load_cutout", side_effect=TimeoutError("same parent unavailable")):
                twenty = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=20))

        self.assertEqual(twenty["status"], "partial")
        self.assertEqual(twenty["zoom_missing_outer_count"], 2)
        self.assertEqual(twenty["frame_count"], 2)
        for index in range(2):
            pixels = self._science(twenty, index)
            np.testing.assert_array_equal(pixels[75:125, 75:125], self._science(five, index))
            self.assertTrue(np.isnan(pixels[0, 0]))
            self.assertEqual(twenty["frames"][index]["source_url"], self.sources[index])
            self.assertEqual(twenty["frames"][index]["zoom_outer_region_status"], "unavailable")

    def test_old_png_only_sequence_recovers_from_same_parent_without_new_selection(self):
        with (
            patch("config.OUTPUT_ROOT", self.root),
            patch("sequence.cached_search_observations", return_value=self.observations) as sia,
            patch("sequence.load_cutout", side_effect=self._load),
            patch("zoom.load_cutout", side_effect=self._load),
            patch("sequence.reproject_interp", side_effect=self._reproject),
            patch("zoom.reproject_interp", side_effect=self._reproject),
        ):
            base = build_sequence(self.request)
            original_science = [self._science(base, i).copy() for i in range(2)]
            folder = self.root / base["sequence_id"]
            base.pop("science_format")
            for frame in base["frames"]:
                (folder / frame.pop("science_file")).unlink()
            (folder / "manifest.json").write_text(json.dumps(base), encoding="utf-8")
            wider = build_zoom(ZoomRequest(sequence_id=base["sequence_id"], fov_arcmin=20))
            restored = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

        sia.assert_called_once()
        self.assertEqual(restored["science_format"], "aligned-npy-v1")
        self.assertEqual(wider["frame_count"], base["frame_count"])
        self.assertEqual(Counter(source for source, _ in self.calls),
                         Counter(self.sources * 3))
        for index in range(2):
            np.testing.assert_array_equal(self._science(wider, index)[75:125, 75:125],
                                          original_science[index])
            self.assertEqual(wider["frames"][index]["source_url"], base["frames"][index]["source_url"])

    def test_retry_partial_zoom_fetches_same_parent_and_fills_only_new_sky(self):
        with (
            patch("config.OUTPUT_ROOT", self.root),
            patch("sequence.cached_search_observations", return_value=self.observations) as sia,
            patch("sequence.load_cutout", side_effect=self._load),
            patch("sequence.reproject_interp", side_effect=self._reproject),
            patch("zoom.reproject_interp", side_effect=self._reproject),
        ):
            five = build_sequence(self.request)
            with patch("zoom.load_cutout", side_effect=TimeoutError("parent temporarily down")):
                failed = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=20))
            failed_science = [self._science(failed, index).copy() for index in range(2)]
            with patch("zoom.load_cutout", side_effect=self._load):
                retried = build_zoom(ZoomRequest(sequence_id=five["sequence_id"], fov_arcmin=20),
                                     refresh=True)
        sia.assert_called_once()
        self.assertEqual(failed["zoom_missing_outer_count"], 2)
        self.assertEqual(retried["zoom_missing_outer_count"], 0)
        self.assertEqual(retried["status"], "ready")
        for index in range(2):
            np.testing.assert_array_equal(self._science(retried, index)[75:125, 75:125],
                                          self._science(five, index))
            self.assertTrue(np.isnan(failed_science[index][0, 0]))
            self.assertTrue(np.isfinite(self._science(retried, index)[0, 0]))
            self.assertEqual(retried["frames"][index]["source_url"], self.sources[index])


if __name__ == "__main__":
    unittest.main()
