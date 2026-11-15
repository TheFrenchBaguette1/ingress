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
from henry_fits import BAD_FLAG_BITS, ParentReadUnavailable
from sequence import SequenceRequest
from sequence_disk_cache import HENRY_FRAME_FILTER, find_saved_sequence
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

            with (
                patch("flipbook_render.read_parent", side_effect=ParentReadUnavailable("bucket unavailable")),
                patch("flipbook_render.load_cutout", side_effect=load),
            ):
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
                self.assertEqual(timeout_seconds, 10)
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
                patch("flipbook_render.monotonic", side_effect=[100, 101, 108]),
                patch("flipbook_render.read_parent", side_effect=ParentReadUnavailable("bucket unavailable")) as native,
                patch("flipbook_render.load_cutout", side_effect=load) as download,
            ):
                frame = writer.align(row, 18)
            self.assertEqual(native.call_args.args[0], source)
            self.assertEqual(native.call_args.kwargs["deadline"], 110)
            self.assertEqual(download.call_count, 2)
            self.assertEqual(frame["source_url"], source)
            self.assertEqual(frame["processing_method"], "irsa_cutout")
            self.assertFalse(frame["quality_mask_applied"])
            self.assertEqual(frame["native_cutout_width_arcmin"], MASTER_WIDTH_ARCMIN)
            self.assertTrue(np.isnan(frame["image"][100, 5]))
            self.assertAlmostEqual(frame["image"][100, 100], 1)
            self.assertLess(frame["coverage_fraction"], 1)
            manifest = writer.manifest([frame], candidate_count=1, attempted_count=1,
                                       failure_count=0, status="ready")
            self.assertEqual(manifest["request"]["fov_arcmin"], MASTER_WIDTH_ARCMIN)
            self.assertIn("after a wider cutout failed", " ".join(manifest["limitations"]))

    @staticmethod
    def native_case(directory):
        request = SimpleNamespace(ra_deg=12.0, dec_deg=75.0, band="D4",
                                  start_date=date(2025, 5, 1), end_date=date(2025, 7, 1))
        source = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/"
                  "2025W23_1A/l2b-v27-2026-215/4/level2_2025W23_1A_0001_1D4_spx_l2b-v27-2026-215.fits")
        values = {"access_url": source, "energy_bandpassname": "SPHEREx-D4",
                  "t_min": Time("2025-06-01T03:04:05.123456").mjd}
        writer = BandWriter(Path(directory), request, "D4")
        native = writer.wcs.deepcopy()
        native.wcs.crpix += np.array([50, 70])
        header = native.to_header()
        header.update(NAXIS=2, NAXIS1=340, NAXIS2=340, BUNIT="Jy / sr")
        return writer, values, header

    def test_native_geometry_masks_flags_before_alignment_without_changing_real_peaks(self):
        with TemporaryDirectory() as directory:
            writer, row, header = self.native_case(directory)
            source = row["access_url"]
            image = np.full((340, 340), 2e6)
            image[40 + 70, 30 + 50] = 15e6
            image[80 + 70, 40 + 50] = np.nan
            flags = np.zeros(image.shape, dtype=np.int64)
            for i, bit in enumerate(BAD_FLAG_BITS):
                flags[110 + 70, 15 + 3 * i + 50] = 1 << bit
            flags[169:171, 149:151] = 1 << 21  # Known source; advisory, not masked.

            def read(source_url, want, *, deadline):
                self.assertEqual(source_url, source)
                self.assertEqual(deadline, 110)
                x0, x1, y0, y1 = want(header)
                self.assertLessEqual(x0, 47)
                self.assertGreaterEqual(x1, 252)
                self.assertLessEqual(y0, 67)
                self.assertGreaterEqual(y1, 272)
                return header, image[y0:y1, x0:x1], flags[y0:y1, x0:x1], x0, y0

            with (
                patch("flipbook_render.monotonic", side_effect=[100, 102]),
                patch("flipbook_render.read_parent", side_effect=read),
                patch("flipbook_render.read_wavelength", return_value=(3.141592, 0.123456)) as wave,
                patch("flipbook_render.load_cutout") as fallback,
            ):
                frame = writer.align(row, 18)
            fallback.assert_not_called()
            self.assertEqual(wave.call_args.args[0], source)
            self.assertAlmostEqual(wave.call_args.args[1], 149.5)
            self.assertAlmostEqual(wave.call_args.args[2], 169.5)
            self.assertEqual(wave.call_args.kwargs["deadline"], 105)
            self.assertEqual(frame["time_mjd"], row["t_min"])
            self.assertEqual(frame["processing_method"], "native_fits_range")
            self.assertTrue(frame["quality_mask_applied"])
            self.assertEqual(frame["center_flag_names"], ["SOURCE"])
            self.assertEqual(frame["center_wavelength_um"], 3.141592)
            self.assertEqual(frame["center_bandwidth_um"], 0.123456)
            self.assertAlmostEqual(frame["image"][40, 30], 15)
            self.assertAlmostEqual(frame["image"][100, 100], 2)
            self.assertTrue(np.isnan(frame["image"][80, 40]))
            for i in range(len(BAD_FLAG_BITS)):
                self.assertTrue(np.isnan(frame["image"][110, 15 + 3 * i]))

    def test_sparse_native_frame_with_masked_center_can_play_without_wavelength(self):
        with TemporaryDirectory() as directory:
            writer, row, header = self.native_case(directory)
            image = np.full((340, 340), np.nan)
            image[95:130, 75:105] = 4e6
            flags = np.zeros(image.shape, dtype=np.int64)
            with (
                patch("flipbook_render.read_parent", return_value=(header, image, flags, 0, 0)),
                patch("flipbook_render.read_wavelength", side_effect=OSError("wavelength unavailable")),
                patch("flipbook_render.load_cutout") as fallback,
            ):
                valid = writer.align(row, 18)
                self.assertTrue(valid["quality_mask_applied"])
                self.assertIsNone(valid["center_wavelength_um"])
                self.assertAlmostEqual(valid["image"][40, 40], 4)
                self.assertTrue(np.isnan(valid["image"][100, 100]))
                self.assertLess(valid["coverage_fraction"], 0.25)

                flags[:] = 1 << 9
                with self.assertRaisesRegex(ValueError, "no playable sampled pixels"):
                    writer.align(row, 18)
            fallback.assert_not_called()

    def test_overflow_and_six_pixel_fringe_fill_only_the_display(self):
        with TemporaryDirectory() as directory:
            writer, row, header = self.native_case(directory)
            image = np.full((340, 340), 2e6)
            image[100, 100] = 15e6
            flags = np.zeros(image.shape, dtype=np.int64)
            flags[110, 80] = (1 << 1) | (1 << 9)  # Saturated core with bad sample.
            flags[110, 86] = 1 << 9               # Six native pixels from the core.
            flags[110, 87] = 1 << 9               # Seven pixels from the core.
            with (
                patch("flipbook_render.read_parent", return_value=(header, image, flags, 0, 0)),
                patch("flipbook_render.read_wavelength", return_value=(3.1, 0.2)),
            ):
                frame = writer.align(row, 18)
            self.assertAlmostEqual(frame["preview_pixels"][40, 30], 15)
            self.assertAlmostEqual(frame["preview_pixels"][40, 36], 15)
            self.assertTrue(np.isnan(frame["preview_pixels"][40, 37]))
            self.assertAlmostEqual(frame["preview_pixels"][120, 120], 2)
            self.assertEqual(frame["preview_fill_pixels"], 2)

            manifest = writer.manifest([frame], candidate_count=1, attempted_count=1,
                                       failure_count=0, status="ready")
            saved = manifest["frames"][0]
            self.assertEqual(manifest["frame_filter"], HENRY_FRAME_FILTER)
            self.assertEqual(saved["preview_fill_pixels"], 2)
            self.assertNotIn("preview_pixels", saved)
            science = np.load(writer.folder / saved["science_file"], allow_pickle=False)
            self.assertTrue(np.isnan(science[40, 30]))
            self.assertTrue(np.isnan(science[40, 36]))
            self.assertAlmostEqual(science[30, 50], 15)
            with Image.open(writer.folder / Path(saved["image_url"]).name) as preview:
                peak = preview.getpixel((30, writer.shape[0] - 1 - 40))
                self.assertGreaterEqual(peak[0], 250)
                self.assertEqual(peak[3], 255)
                self.assertEqual(preview.getpixel((37, writer.shape[0] - 1 - 40))[3], 0)

    def test_weighted_alignment_retains_support_above_henrys_threshold(self):
        with TemporaryDirectory() as directory:
            writer, _, _ = self.native_case(directory)
            writer.shape = (1, 2)
            image = np.array([[5.0, np.nan], [1.0, 1.0]])
            coords = np.array([[0.0, 0.0], [0.39, 0.41]])
            sample = writer._weighted_pixels(image, np.isfinite(image), coords)
            self.assertAlmostEqual(sample[0, 0], 5)
            self.assertTrue(np.isnan(sample[0, 1]))

    def test_native_window_clamps_at_detector_edge_without_filling_unobserved_sky(self):
        with TemporaryDirectory() as directory:
            writer, row, header = self.native_case(directory)
            header["CRPIX1"] -= 120
            image = np.full((340, 340), 7e6)
            flags = np.zeros(image.shape, dtype=np.int64)

            def read(source_url, want, *, deadline):
                x0, x1, y0, y1 = want(header)
                self.assertEqual(x0, 0)
                return header, image[y0:y1, x0:x1], flags[y0:y1, x0:x1], x0, y0

            with (
                patch("flipbook_render.read_parent", side_effect=read),
                patch("flipbook_render.read_wavelength", return_value=(3.1, 0.2)),
                patch("flipbook_render.load_cutout") as fallback,
            ):
                frame = writer.align(row, 18)
            fallback.assert_not_called()
            self.assertTrue(frame["quality_mask_applied"])
            self.assertAlmostEqual(frame["image"][100, 100], 7)
            self.assertTrue(np.isnan(frame["image"][100, 20]))
            self.assertAlmostEqual(frame["coverage_fraction"], 0.65, delta=0.02)
