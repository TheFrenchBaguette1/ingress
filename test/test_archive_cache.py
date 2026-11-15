"""Archive metadata is shared between band searches without sharing mutable rows."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

from archive_cache import (
    _write_disk_listing, cached_search_observations, clear_archive_cache,
    warm_all_band_listing,
)
from archive import ArchiveUnavailable
from sequence import SequenceRequest, build_sequence


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.disk_cache_root = patch("archive_cache.config.OUTPUT_ROOT", Path(self.temporary.name))
        self.disk_cache_root.start()
        self.addCleanup(self.disk_cache_root.stop)
        clear_archive_cache()

    def tearDown(self):
        clear_archive_cache()

    def test_detector_filter_has_distinct_cache_key(self):
        coord = SkyCoord(180, -30, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D3")
        cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D3")
        cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D4")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.kwargs["band"], "D4")

    def test_unfiltered_warm_listing_can_serve_each_band_after_restart(self):
        coord = SkyCoord(12, 75, unit="deg")
        start, end = Time("2025-05-01"), Time("2026-11-15")
        listing = Table(
            [[Time("2026-02-06").mjd, Time("2026-02-07").mjd],
             ["SPHEREx-D1", "SPHEREx-D4"],
             ["https://example.org/d1.fits", "https://example.org/d4.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        warm_all_band_listing(coord, 850, ["dr1", "qr3"], start, end, fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs, {})  # No BAND constraint.
        for band in ("D1", "D4"):
            reused = cached_search_observations(
                coord, 850, ["dr1", "qr3"], start, end, fetch, band=band,
            )
            self.assertTrue(reused.meta["archive_listing_cached"])
            self.assertEqual(len(reused), 2)  # Downstream filters exact detector.
        clear_archive_cache()
        restored = cached_search_observations(
            coord, 850, ["dr1", "qr3"], start, end, fetch, band="D1",
        )
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(restored.meta["archive_listing_cached"])

    def test_filtered_listing_cannot_claim_other_detector_empty(self):
        coord = SkyCoord(12, 75, unit="deg")
        start, end = Time("2025-05-01"), Time("2026-11-15")
        d4_only = Table(
            [[Time("2026-02-06").mjd], ["SPHEREx-D4"],
             ["https://example.org/d4.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(side_effect=[d4_only, RuntimeError("D1 index unavailable")])
        cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D4")
        with self.assertRaisesRegex(RuntimeError, "D1 index unavailable"):
            cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D1")
        self.assertEqual(fetch.call_count, 2)

    def test_incomplete_listing_is_never_cached_as_full_coverage(self):
        coord = SkyCoord(180, -30, unit="deg")
        start, end = Time("2025-05-01"), Time("2026-11-15")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        listing.meta["archive_listing_partial"] = True
        listing.meta["archive_listing_window_note"] = "Only one sampled date window answered."
        fetch = Mock(return_value=listing)
        for _ in range(2):
            result = cached_search_observations(
                coord, 850, ["dr1"], start, end, fetch, band="D3"
            )
            self.assertTrue(result.meta["archive_listing_partial"])
        self.assertEqual(fetch.call_count, 2)

    def test_sia_overflow_is_not_reused_as_complete_all_band_listing(self):
        coord = SkyCoord(12, 75, unit="deg")
        start, end = Time("2025-05-01"), Time("2026-11-15")
        listing = Table(
            [[Time("2026-02-06").mjd], ["SPHEREx-D4"],
             ["https://example.org/d4.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        listing.meta["QUERY_STATUS"] = "OVERFLOW"
        fetch = Mock(return_value=listing)
        warm_all_band_listing(coord, 850, ["dr1"], start, end, fetch)
        cached_search_observations(coord, 850, ["dr1"], start, end, fetch, band="D1")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args.kwargs, {"band": "D1"})

    def test_old_unfiltered_listing_remains_usable_during_outage(self):
        coord = SkyCoord(180, -30, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        old_key = (180.0, -30.0, 850.0, ("dr1",), start.isot, end.isot)
        _write_disk_listing(old_key, listing)
        result = cached_search_observations(
            coord, 850, ["dr1"], start, end,
            Mock(side_effect=RuntimeError("archive down")), band="D3",
        )
        self.assertTrue(result.meta["archive_listing_cached"])
        self.assertEqual(len(result), 1)

    def test_reuses_identical_listing_and_defends_against_mutations(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"]],
            names=["t_min", "energy_bandpassname"],
        )
        fetch = Mock(return_value=listing)

        first = cached_search_observations(coord, 300, ["dr1", "qr3"], start, end, fetch)
        first["energy_bandpassname"][0] = "changed"
        listing["energy_bandpassname"][0] = "also changed"
        second = cached_search_observations(coord, 300, ["dr1", "qr3"], start, end, fetch)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["energy_bandpassname"][0], "SPHEREx-D4")

    def test_query_key_includes_sky_size_collections_and_dates(self):
        coord = SkyCoord(120, -85, unit="deg")
        later = SkyCoord(121, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        fetch = Mock(return_value=Table(names=["t_min"], dtype=[float]))

        for args in (
            (coord, 300, ["dr1"], start, end),
            (later, 300, ["dr1"], start, end),
            (coord, 600, ["dr1"], start, end),
            (coord, 300, ["dr1", "qr3"], start, end),
            (coord, 300, ["dr1"], start, Time("2025-07-01")),
        ):
            cached_search_observations(*args, fetch=fetch)
        self.assertEqual(fetch.call_count, 5)

    def test_failures_are_not_cached(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        fetch = Mock(side_effect=[OSError("IRSA unavailable"), Table(names=["t_min"], dtype=[float])])
        with self.assertRaises(OSError):
            cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_successful_listing_survives_process_restart_for_other_band(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd, Time("2025-05-04").mjd],
             ["SPHEREx-D3", "SPHEREx-D4"],
             ["https://example.org/three.fits", "https://example.org/four.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        first = cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        clear_archive_cache()  # Recreate the API process before another band.
        second = cached_search_observations(coord, 300, ["dr1"], start, end, fetch)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["energy_bandpassname"].tolist(), ["SPHEREx-D3", "SPHEREx-D4"])
        second["access_url"][0] = "changed"
        self.assertEqual(first["access_url"][0], "https://example.org/three.fits")

    def test_saved_listing_expires_and_fetches_again(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        saved = next((Path(self.temporary.name) / ".archive-listings").glob("*.json"))
        payload = json.loads(saved.read_text(encoding="utf-8"))
        payload["saved_at"] = 0
        saved.write_text(json.dumps(payload), encoding="utf-8")
        clear_archive_cache()
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_in_process_listing_expires_too(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        saved = next((Path(self.temporary.name) / ".archive-listings").glob("*.json"))
        payload = json.loads(saved.read_text(encoding="utf-8"))
        payload["saved_at"] = 0  # Simulate a stale on-disk listing too.
        saved.write_text(json.dumps(payload), encoding="utf-8")
        with patch("archive_cache.unix_time", return_value=time.time() + 3601):
            cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_refresh_requeries_even_with_warm_listing(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch, band="D4")
        cached_search_observations(
            coord, 300, ["dr1"], start, end, fetch, band="D4", refresh=True,
        )
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch, band="D4")
        self.assertEqual(fetch.call_count, 2)

    def test_saved_listing_requires_exact_sky_and_utc_interval(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        clear_archive_cache()
        cached_search_observations(SkyCoord(121, -85, unit="deg"), 300, ["dr1"], start, end, fetch)
        cached_search_observations(coord, 300, ["dr1"], start, Time("2025-07-01"), fetch)
        self.assertEqual(fetch.call_count, 3)

    def test_corrupt_saved_listing_never_blocks_archive_lookup(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        listing = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        fetch = Mock(return_value=listing)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        saved = next((Path(self.temporary.name) / ".archive-listings").glob("*.json"))
        saved.write_text("{broken", encoding="utf-8")
        clear_archive_cache()
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_empty_listing_is_not_cached(self):
        coord = SkyCoord(120, -85, unit="deg")
        start, end = Time("2025-05-01"), Time("2025-06-01")
        fetch = Mock(return_value=Table(names=["t_min"], dtype=[float]))
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        cached_search_observations(coord, 300, ["dr1"], start, end, fetch)
        self.assertEqual(fetch.call_count, 2)

    def test_slow_archive_query_consumes_the_build_budget(self):
        request = SequenceRequest(
            ra_deg=111.11, dec_deg=-85, start_date="2025-05-01",
            end_date="2025-06-01", fov_arcmin=5, band="D4",
        )
        observations = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D4"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        clock = [0.0]

        def slow_search(*_args, **_kwargs):
            clock[0] = 121.0
            return observations

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.search_observations", side_effect=slow_search),
            patch("sequence.monotonic", side_effect=lambda: clock[0]),
            patch("sequence.load_cutout") as load,
        ):
            with self.assertRaisesRegex(ArchiveUnavailable, "preparation time budget ended"):
                build_sequence(request)

        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
