"""Archive metadata is shared between band searches without sharing mutable rows."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

from archive_cache import cached_search_observations, clear_archive_cache
from sequence import SequenceRequest, build_sequence


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        clear_archive_cache()

    def tearDown(self):
        clear_archive_cache()

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

        def slow_search(*_args):
            clock[0] = 121.0
            return observations

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.search_observations", side_effect=slow_search),
            patch("sequence.monotonic", side_effect=lambda: clock[0]),
            patch("sequence.load_cutout") as load,
        ):
            manifest = build_sequence(request)

        load.assert_not_called()
        self.assertEqual(manifest["attempted_count"], 0)
        self.assertEqual(manifest["skipped_count"], 1)


if __name__ == "__main__":
    unittest.main()
