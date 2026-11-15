"""Regression coverage for the direct SPHEREx parent range reader."""

from io import BytesIO
from time import monotonic
import unittest
from unittest.mock import Mock, patch

from astropy import units as u
from astropy.io import fits
import numpy as np

from henry_fits import ParentReadUnavailable, S3_ROOT, read_parent, read_wavelength


PRODUCT = "dr1/level2/2025W23_1A/l2b-v27-2026-215/4/level2_2025W23_1A_0001_1D4_spx_l2b-v27-2026-215.fits"
SOURCE = "https://irsa.ipac.caltech.edu/ibe/data/spherex/" + PRODUCT


class MemoryRangeSession:
    def __init__(self, payload, *, status=206, wrong_range=False):
        self.payload = payload
        self.calls = []
        self.status = status
        self.wrong_range = wrong_range

    def get(self, url, *, headers, stream, allow_redirects, timeout):
        self.calls.append((url, headers))
        assert stream and not allow_redirects and min(timeout) > 0
        range_spec = headers["Range"].removeprefix("bytes=")
        start, end = range_spec.split("-")
        total = len(self.payload)
        if start:
            low, high = int(start), min(total, int(end) + 1)
        else:
            low, high = max(0, total - int(end)), total
        data = self.payload[low:high]
        if self.wrong_range:
            low += 1
        response = Mock(status_code=self.status, headers={
            "Content-Range": f"bytes {low}-{high - 1}/{total}",
            "Content-Length": str(len(data)),
            "ETag": '"version-id"',
        })
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content = Mock(return_value=[data])
        self.response = response
        return response


class ParentFitsTests(unittest.TestCase):
    def setUp(self):
        self.science = np.arange(32 * 50, dtype=np.float32).reshape(32, 50) + 0.125
        self.science[8, 9] = np.nan
        self.flags = (np.arange(32 * 50, dtype=np.int32).reshape(32, 50) << 12) | 1
        tail = fits.BinTableHDU.from_columns([
            fits.Column(name="VALUES", format="2D", array=[[3.123456789, 0.02123456789]]),
        ], name="WCS-WAVE")
        hdus = fits.HDUList([
            fits.PrimaryHDU(),
            fits.ImageHDU(self.science, name="IMAGE"),
            fits.CompImageHDU(self.flags, name="FLAGS", compression_type="RICE_1",
                              tile_shape=(1, 50)),
            tail,
        ])
        file = BytesIO()
        hdus.writeto(file)
        self.payload = file.getvalue()

    def test_compressed_flags_and_unmodified_science_subset(self):
        session = MemoryRangeSession(self.payload)
        with patch("henry_fits._session", return_value=session):
            hdr, science, flags, x0, y0 = read_parent(
                SOURCE, lambda h: (7, 22, 5, 14), deadline=monotonic() + 5,
            )

        self.assertEqual((x0, y0), (7, 5))
        self.assertEqual(hdr["EXTNAME"], "IMAGE")
        np.testing.assert_array_equal(science, self.science[5:14, 7:22])
        np.testing.assert_array_equal(flags, self.flags[5:14, 7:22])
        self.assertTrue(all(url == S3_ROOT + PRODUCT for url, _ in session.calls))
        self.assertTrue(all(headers["If-Match"] == '"version-id"' for _, headers in session.calls[1:]))

    def test_non_206_and_mismatched_range_are_rejected_without_reading_body(self):
        for session in (MemoryRangeSession(self.payload, status=200),
                        MemoryRangeSession(self.payload, wrong_range=True)):
            with patch("henry_fits._session", return_value=session):
                with self.assertRaises(ParentReadUnavailable):
                    read_parent(SOURCE, lambda header: (1, 3, 2, 4), deadline=monotonic() + 5)
            session.response.iter_content.assert_not_called()

    def test_wavelength_uses_actual_tail_and_native_fractional_coordinates(self):
        session = MemoryRangeSession(self.payload)
        original = fits.Header()
        original["NAXIS1"], original["NAXIS2"] = 50, 32
        wcs = Mock()

        def decode(header, *, fobj, key):
            self.assertIs(header, original)
            self.assertEqual(key, "W")
            values = fobj["WCS-WAVE"].data["VALUES"][0]
            wcs.pixel_to_world.return_value = values * u.um
            return wcs

        with (
            patch("henry_fits._session", return_value=session),
            patch("henry_fits.WCS", side_effect=decode) as constructor,
        ):
            values = read_wavelength(SOURCE, 12.375, 23.625, original, deadline=monotonic() + 5)
        constructor.assert_called_once()
        wcs.pixel_to_world.assert_called_once_with(12.375, 23.625)
        self.assertEqual(values, (3.123456789, 0.02123456789))
        self.assertTrue(session.calls[0][1]["Range"].startswith("bytes=-"))
