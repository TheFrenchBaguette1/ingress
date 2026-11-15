"""Previously verified real image URLs can outlive the image search index."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astropy.coordinates import SkyCoord
from astropy.time import Time

from archive import search_observations
import archive
from saved_sources import find_saved_sources


class SavedSourceTests(unittest.TestCase):
    def setUp(self):
        archive._recent_failures.clear()
        self.addCleanup(archive._recent_failures.clear)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / ("a" * 32)
        self.folder.mkdir()
        (self.folder / "frame_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        self.url = (
            "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/"
            "2025W22_1B/l2b-v27-2026-199/3/real_d3.fits"
        )
        self.mjd = Time("2025-06-01").mjd
        self.manifest = {
            "status": "ready", "frame_count": 1,
            "request": {"ra_deg": 180.0, "dec_deg": -30.0, "band": "D3",
                        "fov_arcmin": 5.0},
            "frames": [{"time_mjd": self.mjd, "detector": "SPHEREx-D3",
                        "source_url": self.url,
                        "image_url": "/outputs/" + "a" * 32 + "/frame_000.png"}],
        }
        self._write()
        self.coord = SkyCoord(180, -30, unit="deg")
        self.start, self.end = Time("2025-05-01"), Time("2025-07-01")

    def _write(self):
        (self.folder / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def _search(self, coord=None, band="D3", start=None, end=None):
        return find_saved_sources(
            self.coord if coord is None else coord, band,
            self.start if start is None else start,
            self.end if end is None else end,
            output_root=self.root, bundled_root=self.root / "no-bundle",
        )

    def test_exact_center_detector_and_date_recover_real_source(self):
        result = self._search()
        self.assertEqual(len(result), 1)
        self.assertEqual(result["access_url"][0], self.url)
        self.assertTrue(result.meta["archive_listing_partial"])
        self.assertIsNone(self._search(coord=SkyCoord(180.0001, -30, unit="deg")))
        self.assertIsNone(self._search(band="D4"))
        self.assertIsNone(self._search(start=Time("2025-06-02")))

    def test_untrusted_urls_and_missing_real_frame_are_rejected(self):
        self.manifest["frames"][0]["source_url"] = (
            "https://irsa.ipac.caltech.edu.evil.example/ibe/data/spherex/dr1/"
            "level2/2025W22_1B/l2b-v27-2026-199/3/real_d3.fits"
        )
        self._write()
        self.assertIsNone(self._search())
        self.manifest["frames"][0]["source_url"] = self.url
        self._write()
        (self.folder / "frame_000.png").unlink()
        self.assertIsNone(self._search())

    def test_broad_failure_uses_saved_source_before_more_queries(self):
        with (
            patch("archive._outage_until", 0.0),
            patch("archive._outage_probe", False),
            patch("archive._BoundedIrsa.query_sia",
                  side_effect=RuntimeError("502 Proxy Error")) as query,
        ):
            result = search_observations(
                self.coord, 850, ["spherex_dr1"], self.start, self.end,
                band="D3", known_sources=self._search,
            )
        self.assertEqual(query.call_count, 1)
        self.assertEqual(len(result), 1)
        self.assertTrue(result.meta["archive_listing_partial"])

    def test_recent_failure_reuses_source_briefly_then_checks_fresh_index(self):
        clock = [100.0]
        fresh = self._search().copy()
        fresh.meta.clear()
        with (
            patch("archive.monotonic", side_effect=lambda: clock[0]),
            patch("archive._outage_until", 0.0),
            patch("archive._outage_probe", False),
            patch("archive._BoundedIrsa.query_sia", side_effect=[
                RuntimeError("502 Proxy Error"), fresh,
            ]) as query,
        ):
            def lookup():
                return search_observations(
                    self.coord, 850, ["spherex_dr1"], self.start, self.end,
                    band="D3", known_sources=self._search,
                )

            self.assertTrue(lookup().meta["archive_listing_partial"])
            self.assertTrue(lookup().meta["archive_listing_partial"])
            self.assertEqual(query.call_count, 1)
            clock[0] += archive.KNOWN_SOURCE_RETRY_SECONDS + 1
            self.assertFalse(lookup().meta.get("archive_listing_partial"))
            self.assertEqual(query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
