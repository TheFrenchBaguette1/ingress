"""Native detector stamps and release calibration for stellar photometry."""

from io import BytesIO
import unittest
from unittest.mock import patch

from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS
import numpy as np

import photometry_inputs as inputs


SOURCE = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W18_1B/"
          "l2b-v27-2026-196/4/level2_2025W18_1B_0023_2D4_spx_l2b-v27-2026-196.fits")


def payload(*hdus):
    buffer = BytesIO()
    fits.HDUList([fits.PrimaryHDU(), *hdus]).writeto(buffer)
    return buffer.getvalue()


def plane(name, data, unit=None):
    hdu = fits.ImageHDU(data, name=name)
    if unit:
        hdu.header["BUNIT"] = unit
    return hdu


def wave_table(values=(2.31, 2.38), offset=(0, 0)):
    return fits.BinTableHDU.from_columns([
        fits.Column(name="X", format="2D", array=[[1 - offset[0], 20 - offset[0]]]),
        fits.Column(name="Y", format="2D", array=[[1 - offset[1], 20 - offset[1]]]),
        fits.Column(name="VALUES", format="2D", array=[values]),
    ], name="WCS-WAVE")


def effective_psf():
    y, x = np.mgrid[-16:17, -16:17]
    narrow = np.exp(-(x**2 + y**2) / 30.)
    broad = np.exp(-(x**2 + y**2) / 75.)
    hdu = fits.BinTableHDU.from_columns([
        fits.Column(name="XCENTER", format="E", array=[2., 15.]),
        fits.Column(name="YCENTER", format="E", array=[9., 9.]),
        fits.Column(name="EPSF", format="1089D", dim="(33,33)", array=[narrow, broad]),
    ], name="EPSF")
    hdu.header["OVSMPX"] = hdu.header["OVSMPY"] = 5
    return hdu


class _MemoryReader:
    def __init__(self, content):
        self.content = content
        self.size = len(content)
        self.largest_read = 0

    def read(self, first, end):
        self.largest_read = max(self.largest_read, end - first)
        return self.content[first:end]


class PhotometryInputTest(unittest.TestCase):
    def setUp(self):
        self.n = 20
        y, x = np.indices((self.n, self.n))
        self.science = (10 * y + x).astype("float32")
        self.variance = (0.1 + x / 100).astype("float32")
        self.flags = np.zeros_like(x, dtype=np.int32)
        self.flags[9, 13] = 1 << 21
        self.wave = (2 + x / 100 + y / 200).astype("float32")
        self.width = (0.1 + x / 1000).astype("float32")
        self.area = (36 + y / 100).astype("float32")
        self.channel = np.full_like(x, 3, dtype=np.int16)
        self.channel[:, 10:] = 5
        self.wcs = WCS(naxis=2)
        self.wcs.wcs.crpix = [10., 10.]
        self.wcs.wcs.crval = [250., 65.]
        self.wcs.wcs.cdelt = [-6.15 / 3600, 6.15 / 3600]
        self.wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        self.xy = (11.25, 9.625)
        self.coord = self.wcs.pixel_to_world(*self.xy)
        self.channels_hdu = fits.BinTableHDU.from_columns([
            fits.Column(name="DETECTOR", format="J", array=[4, 4]),
            fits.Column(name="SUBCHAN", format="J", array=[3, 5]),
            fits.Column(name="WAVELENGTH", format="D", array=[2.21, 2.35]),
            fits.Column(name="BANDWIDTH", format="D", array=[0.10, 0.12]),
        ], name="SPECTRAL_CHANNELS")
        self.source = self.science_payload()
        self.readers = []
        self.patches = [patch.object(inputs, "_DETECTOR_SIZE", self.n),
                        patch.object(inputs, "_PSF_TABLES", {}),
                        patch.object(inputs, "_SPECTRAL_GRIDS", {}),
                        patch.object(inputs, "_CHANNEL_TABLES", {}),
                        patch.object(inputs, "_RangeReader", side_effect=self.reader)]
        for replaced in self.patches:
            replaced.start()
            self.addCleanup(replaced.stop)

    def science_payload(self, offset=(0, 0), size=20, wave_values=(2.31, 2.38)):
        x0, y0 = offset
        region = np.s_[y0:y0 + size, x0:x0 + size]
        science = plane("IMAGE", self.science[region], "MJy / sr")
        science.header.update(self.wcs.to_header())
        science.header["CRPIX1"] -= x0
        science.header["CRPIX2"] -= y0
        science.header["CRPIX1A"] = 1 - x0
        science.header["CRPIX2A"] = 1 - y0
        science.header["MJD-AVG"] = 60792.5
        return payload(science, plane("FLAGS", self.flags[region]),
                       plane("VARIANCE", self.variance[region], "MJy2 / sr2"),
                       effective_psf(), wave_table(wave_values, offset))

    def reader(self, url, deadline, *, calibration=False):
        if not calibration:
            self.assertEqual(SOURCE, url)
            content = self.source
        elif "/spectral_wcs/" in url:
            content = payload(plane("CWAVE", self.wave, "um"), plane("CBAND", self.width, "um"), wave_table())
        elif "/spectral_channels/" in url:
            channel_hdus = () if self.channels_hdu is None else (self.channels_hdu,)
            content = payload(plane("CHANNEL-MAP", self.channel), *channel_hdus)
        else:
            self.assertIn("/solid_angle_pixel_map/", url)
            content = payload(plane("IMAGE", self.area, "arcsec2"))
        reader = _MemoryReader(content)
        self.readers.append(reader)
        return reader

    def load(self, coord=None):
        coord = self.coord if coord is None else coord
        return inputs.load_photometry_inputs(SOURCE, coord.ra.deg, coord.dec.deg)

    def test_same_native_pixels_variance_flags_and_calibration(self):
        stamp = self.load()
        np.testing.assert_array_equal(stamp.image, self.science[5:16, 6:17])
        np.testing.assert_array_equal(stamp.variance, self.variance[5:16, 6:17])
        np.testing.assert_array_equal(stamp.flags, self.flags[5:16, 6:17])
        np.testing.assert_allclose(stamp.solid_angle_sr, self.area[5:16, 6:17] * (u.arcsec**2).to(u.sr), rtol=1e-7)
        self.assertEqual((stamp.detector, stamp.channel, stamp.mjd), (4, 5, 60792.5))
        self.assertAlmostEqual(stamp.wavelength_um, 2 + 11.25 / 100 + 9.625 / 200, places=6)
        self.assertAlmostEqual(stamp.bandwidth_um, 0.1 + 11.25 / 1000, places=7)
        self.assertEqual((stamp.channel_wavelength_um, stamp.channel_bandwidth_um), (2.35, 0.12))
        np.testing.assert_allclose(stamp.target_xy, (5.25, 4.625), atol=1e-9)
        self.assertAlmostEqual(stamp.psf.sum(), 1.)
        self.assertEqual(np.unravel_index(stamp.psf.argmax(), stamp.psf.shape), (5, 5))

    def test_edge_coverage_keeps_original_missing_mask_and_full_psf(self):
        stamp = self.load(self.wcs.pixel_to_world(0.4, 10.1))
        self.assertTrue(np.isnan(stamp.image[:, :5]).all())
        self.assertTrue(np.isnan(stamp.variance[:, :5]).all())
        self.assertTrue(np.isnan(stamp.solid_angle_sr[:, :5]).all())
        self.assertTrue(np.all(stamp.flags[:, :5] == 1 << 9))
        self.assertGreater(stamp.psf[:, :5].sum(), 0)
        self.assertAlmostEqual(stamp.psf.sum(), 1.)

    def test_visualization_wavelength_table_cannot_replace_mismatched_calibration(self):
        self.source = self.science_payload(wave_values=(2.3, 2.4))
        with self.assertRaises(inputs.PhotometryInputUnavailable) as caught:
            self.load()
        self.assertEqual(caught.exception.code, "calibration_unavailable")

    def test_channel_map_keeps_measured_wavelength_with_published_nominal_fallback(self):
        self.channels_hdu = None
        stamp = self.load()
        self.assertEqual(stamp.channel, 5)
        self.assertEqual(stamp.channel_wavelength_um, 2.719)
        self.assertAlmostEqual(stamp.channel_bandwidth_um, 2.719 / 35)
        self.assertAlmostEqual(stamp.wavelength_um, 2 + 11.25 / 100 + 9.625 / 200, places=6)

    def test_cutout_fallback_keeps_parent_pixel_coordinates_and_the_same_science(self):
        expected = self.load()
        cutout = self.science_payload(offset=(4, 3), size=15)

        class Response:
            status_code = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, chunk_size):
                yield cutout

        with (patch.object(inputs, "_read_s3_stamp", side_effect=inputs.ParentReadUnavailable),
              patch.object(inputs, "_session") as session):
            session.return_value.get.return_value = Response()
            actual = self.load()
        url = session.return_value.get.call_args.args[0]
        self.assertTrue(url.startswith(SOURCE + "?center="))
        np.testing.assert_array_equal(actual.image, expected.image)
        np.testing.assert_allclose(actual.psf, expected.psf, atol=1e-10)
        self.assertAlmostEqual(actual.wavelength_um, expected.wavelength_um, places=12)

    def test_unknown_parent_and_outside_detector_do_not_use_cutout(self):
        with patch.object(inputs, "_read_cutout_stamp") as fallback:
            with self.assertRaises(inputs.PhotometryInputUnavailable) as outside:
                self.load(self.wcs.pixel_to_world(24, 10))
            with self.assertRaises(inputs.PhotometryInputUnavailable) as unsupported:
                inputs.load_photometry_inputs(SOURCE.replace("dr1/", "qr2/"), 250., 65.)
        self.assertEqual(outside.exception.code, "outside_detector")
        self.assertEqual(unsupported.exception.code, "unsupported_product")
        fallback.assert_not_called()
