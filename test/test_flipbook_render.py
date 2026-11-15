"""An exposure that finishes late must keep every existing image URL valid."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astropy.time import Time
import numpy as np
from PIL import Image

from flipbook_render import (
    BandWriter, CUTOUT_WIDTH_ARCMIN, MASTER_WIDTH_ARCMIN, PROGRESSIVE_PIPELINE,
)
from sequence import SequenceRequest
from sequence_disk_cache import find_saved_sequence
from zoom import ZoomRequest, build_zoom


class ProgressiveFrameTests(unittest.TestCase):
    def test_late_insertion_keeps_the_earlier_preview_and_science_scale(self):
        request = SimpleNamespace(ra_deg=12.0, dec_deg=75.0, band="D4",
                                  start_date=date(2025, 5, 1), end_date=date(2025, 7, 1))
        archive = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W23_1A/l2b-v27-2026-215"
        names = ["level2_2025W23_1A_0001_1D4_spx_l2b-v27-2026-215.fits",
                 "level2_2025W23_1A_0002_1D4_spx_l2b-v27-2026-215.fits"]
        early = {"access_url": f"{archive}/4/{names[0]}", "energy_bandpassname": "SPHEREx-D4",
                 "t_min": Time("2025-05-01").mjd}
        late = {"access_url": f"{archive}/4/{names[1]}", "energy_bandpassname": "SPHEREx-D4",
                "t_min": Time("2025-06-01").mjd}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            writer = BandWriter(root, request, "D4")

            def load(observation, coord, width, *, cache, timeout_seconds):
                self.assertEqual(width, CUTOUT_WIDTH_ARCMIN)
                self.assertGreater(width, 28.28)
                mjd = observation["t_min"]
                pixels = np.full(writer.shape, 3e6 if observation is early else 2e6)
                pixels[0, :] = np.nan
                return {
                    "image": pixels, "wcs": writer.wcs.deepcopy(),
                    "target_x": 99.5, "target_y": 99.5,
                    "detector": "SPHEREx-D4", "wavelength_um": 3.1, "bandwidth_um": 0.2,
                    "time_mjd": mjd, "date_utc": Time(mjd, format="mjd").iso,
                    "brightness_unit": "Jy / sr", "source_url": observation["access_url"],
                    "cutout_url": observation["access_url"] + "?size=0.5",
                }

            with patch("flipbook_render.load_cutout", side_effect=load):
                ready = writer.align(late, 5)
                partial = writer.manifest([ready], candidate_count=2, attempted_count=1,
                                          failure_count=0, status="partial", complete=False)
                stored_request = SequenceRequest(**partial["request"])
                self.assertIsNone(find_saved_sequence(stored_request, root,
                                                      pipeline=PROGRESSIVE_PIPELINE))
                later_path = writer.folder / "frame_000.png"
                first_preview = later_path.read_bytes()
                final = writer.manifest([writer.align(early, 5)], candidate_count=2,
                                        attempted_count=2, failure_count=0, status="ready")

            self.assertEqual(final["display"], partial["display"])
            self.assertEqual(final["frames"][0]["product_id"], names[0])
            self.assertEqual(final["frames"][1]["image_url"], partial["frames"][0]["image_url"])
            self.assertEqual(later_path.read_bytes(), first_preview)
            self.assertEqual([frame["index"] for frame in final["frames"]], [1, 0])

            reused = find_saved_sequence(stored_request, root, pipeline=PROGRESSIVE_PIPELINE)
            self.assertEqual(reused["frames"], final["frames"])
            with patch("config.OUTPUT_ROOT", root):
                zoom = build_zoom(ZoomRequest(sequence_id=writer.sequence_id, fov_arcmin=5))
            self.assertEqual([frame["index"] for frame in zoom["frames"]], [1, 0])
            for value, frame, cropped in zip((3, 2), final["frames"], zoom["frames"]):
                original = np.load(writer.folder / frame["science_file"], allow_pickle=False)
                derived = np.load(writer.folder / "zoom_005" / cropped["science_file"], allow_pickle=False)
                np.testing.assert_array_equal(derived, original[75:125, 75:125])
                self.assertAlmostEqual(derived[25, 25], value)
                with Image.open(writer.folder / Path(frame["image_url"]).name) as preview:
                    np.testing.assert_array_equal(np.asarray(preview)[::-1, :, 3] == 0,
                                                  ~np.isfinite(original))

    def test_mislabeled_archive_products_are_rejected_before_download(self):
        request = SimpleNamespace(ra_deg=12.0, dec_deg=75.0, band="D4",
                                  start_date=date(2025, 5, 1), end_date=date(2025, 7, 1))
        archive = "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W23_1A/l2b-v27-2026-215"
        template = "{folder}/level2_2025W23_1A_0001_1{band}_spx_l2b-v27-2026-215.fits"

        with TemporaryDirectory() as directory, patch("flipbook_render.load_cutout") as load:
            writer = BandWriter(Path(directory), request, "D4")
            for folder, band in ((4, "D3"), (3, "D4")):
                with self.subTest(folder=folder, band=band):
                    row = {
                        "access_url": f"{archive}/{template.format(folder=folder, band=band)}",
                        "energy_bandpassname": "SPHEREx-D4",
                        "t_min": Time("2025-05-01").mjd,
                    }
                    with self.assertRaisesRegex(ValueError, "different SPHEREx detector"):
                        writer.align(row, 5)
            load.assert_not_called()

    def test_original_width_retries_same_parent_and_masks_uncaptured_pixels(self):
        request = SimpleNamespace(ra_deg=12.0, dec_deg=75.0, band="D4",
                                  start_date=date(2025, 5, 1), end_date=date(2025, 7, 1))
        source = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/"
                  "2025W23_1A/l2b-v27-2026-215/4/level2_2025W23_1A_0001_1D4_spx_l2b-v27-2026-215.fits")
        mjd = Time("2025-06-01").mjd
        row = {"access_url": source, "energy_bandpassname": "SPHEREx-D4", "t_min": mjd}
        with TemporaryDirectory() as directory:
            writer = BandWriter(Path(directory), request, "D4")

            def load(observation, coord, width, *, cache, timeout_seconds):
                self.assertIs(observation, row)
                if width == CUTOUT_WIDTH_ARCMIN:
                    self.assertEqual(timeout_seconds, 7)
                    raise OSError("archive HTTP 503")
                self.assertEqual(width, MASTER_WIDTH_ARCMIN)
                self.assertEqual(timeout_seconds, 11)
                pixels = np.ones(writer.shape)
                pixels[:, :40] = np.nan
                return {
                    "image": pixels, "wcs": writer.wcs.deepcopy(),
                    "target_x": 99.5, "target_y": 99.5, "detector": "SPHEREx-D4",
                    "wavelength_um": 3.1, "bandwidth_um": 0.2, "time_mjd": mjd,
                    "date_utc": Time(mjd, format="mjd").iso, "brightness_unit": "MJy / sr",
                    "source_url": source, "cutout_url": source + "?size=0.3333333333333333",
                }

            with (
                patch("flipbook_render.monotonic", side_effect=[100, 107]),
                patch("flipbook_render.load_cutout", side_effect=load) as download,
            ):
                frame = writer.align(row, 18)
            self.assertEqual(download.call_count, 2)
            self.assertEqual(frame["source_url"], source)
            self.assertEqual(frame["native_cutout_width_arcmin"], MASTER_WIDTH_ARCMIN)
            self.assertTrue(np.isnan(frame["image"][100, 5]))
            self.assertAlmostEqual(frame["image"][100, 100], 1)
            self.assertLess(frame["coverage_fraction"], 1)
            manifest = writer.manifest([frame], candidate_count=1, attempted_count=1,
                                       failure_count=0, status="ready")
            self.assertEqual(manifest["request"]["fov_arcmin"], MASTER_WIDTH_ARCMIN)
            self.assertIn("after a wider cutout failed", " ".join(manifest["limitations"]))
