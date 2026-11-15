"""A band switch must use verified same-detector products or disclose outage."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest.mock import patch

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time
from requests.exceptions import Timeout

import archive


class BandFallbackTests(unittest.TestCase):
    def setUp(self):
        archive._tap_results.clear()
        self.addCleanup(archive._tap_results.clear)
        cooldown = patch("archive._tap_failure_until", 0.0)
        cooldown.start()
        self.addCleanup(cooldown.stop)
        outage = patch("archive._outage_until", 0.0)
        outage.start()
        self.addCleanup(outage.stop)
        probe = patch("archive._outage_probe", False)
        probe.start()
        self.addCleanup(probe.stop)
        self.coord = SkyCoord(12, 75, unit="deg")
        self.start, self.end = Time("2025-05-01"), Time("2026-11-15")

    def test_smaller_center_lookup_recovers_real_same_band_products_only(self):
        d1 = Table(
            [[Time("2026-02-06").mjd], ["SPHEREx-D1"],
             ["https://irsa.ipac.caltech.edu/real-d1.fits"]],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=[
                Timeout("wide field timed out"), d1,
            ]) as query,
            patch("archive._tap_lookup") as tap,
        ):
            result = archive.search_observations(
                self.coord, 424.264, ["spherex_dr1", "spherex_qr3"],
                self.start, self.end, band="D1",
            )
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args.kwargs["pos"][1], 75 * u.arcsec)
        self.assertEqual(query.call_args.kwargs["band"][0].to_value(u.um), 0.744)
        tap.assert_not_called()
        self.assertEqual(result["energy_bandpassname"][0], "SPHEREx-D1")
        self.assertTrue(result.meta["archive_listing_partial"])
        self.assertIn("75 arcseconds", result.meta["archive_listing_window_note"])

    def test_adjacent_detector_at_center_does_not_masquerade_as_d1(self):
        d2_only = Table(
            [[Time("2026-02-06").mjd], ["SPHEREx-D2"],
             ["https://irsa.ipac.caltech.edu/real-d2.fits"]],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        d1 = Table(
            [[Time("2026-02-07").mjd], ["SPHEREx-D1"],
             ["https://irsa.ipac.caltech.edu/real-d1.fits"]],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=[
                Timeout("wide field timed out"), d2_only,
            ]),
            patch("archive._tap_lookup", return_value=d1) as tap,
        ):
            result = archive.search_observations(
                self.coord, 424.264, ["spherex_dr1"],
                self.start, self.end, band="D1",
            )
        tap.assert_called_once()
        self.assertEqual(result["energy_bandpassname"].tolist(), ["SPHEREx-D1"])

    def test_tap_point_query_validates_release_host_detector_and_date(self):
        base = "ibe/data/spherex/dr1/level2/2026W06_2A/l2b-v27-2026-222/"
        rows = Table(
            rows=[
                (base + "1/real_d1.fits", Time("2026-02-06").mjd, "SPHEREx-D1"),
                (base + "4/wrong_directory.fits", Time("2026-02-07").mjd, "SPHEREx-D1"),
                ("https://evil.example/" + base + "1/evil.fits", Time("2026-02-08").mjd, "SPHEREx-D1"),
                (base + "1/late.fits", Time("2027-01-01").mjd, "SPHEREx-D1"),
                (base + "4/real_d4.fits", Time("2026-02-06").mjd, "SPHEREx-D4"),
            ], names=("uri", "time_bounds_lower", "energy_bandpassname"),
        )
        clock = [100.0]
        with (
            patch("archive.monotonic", side_effect=lambda: clock[0]),
            patch("archive._BoundedIrsa.query_tap", return_value=rows) as query,
        ):
            d1 = archive._tap_lookup(
                self.coord, ["spherex_dr1", "spherex_qr3"],
                self.start, self.end, "D1", 125.0,
            )
            d4 = archive._tap_lookup(
                self.coord, ["spherex_dr1", "spherex_qr3"],
                self.start, self.end, "D4", 125.0,
            )
        self.assertEqual(query.call_count, 1)  # One TAP probe serves other bands.
        self.assertEqual(query.call_args.kwargs["maxrec"], archive.TAP_MAX_ROWS)
        self.assertIn("CONTAINS(POINT('ICRS',12.000000000000,75.000000000000)",
                      query.call_args.args[0])
        self.assertEqual(len(d1), 1)
        self.assertEqual(len(d4), 1)
        self.assertTrue(d1.meta["archive_listing_partial"])
        self.assertEqual(d1["access_url"][0],
                         "https://irsa.ipac.caltech.edu/" + base + "1/real_d1.fits")
        self.assertEqual(d4["energy_bandpassname"][0], "SPHEREx-D4")

    def test_failed_tap_probe_cools_down_across_bands(self):
        clock = [100.0]
        with (
            patch("archive.monotonic", side_effect=lambda: clock[0]),
            patch("archive._BoundedIrsa.query_tap", side_effect=Timeout("TAP unavailable")) as query,
        ):
            self.assertIsNone(archive._tap_lookup(
                self.coord, ["spherex_dr1"], self.start, self.end, "D1", 125.0))
            self.assertIsNone(archive._tap_lookup(
                self.coord, ["spherex_dr1"], self.start, self.end, "D4", 125.0))
            self.assertEqual(query.call_count, 1)

    def test_two_band_workers_share_one_inflight_tap_query(self):
        base = "ibe/data/spherex/dr1/level2/2026W06_2A/l2b-v27-2026-222/"
        rows = Table(rows=[
            (base + "1/real_d1.fits", Time("2026-02-06").mjd, "SPHEREx-D1"),
            (base + "4/real_d4.fits", Time("2026-02-06").mjd, "SPHEREx-D4"),
        ], names=("uri", "time_bounds_lower", "energy_bandpassname"))
        entered = Event()
        release = Event()

        def slow_tap(*_args, **_kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise Timeout("test TAP query was not released")
            return rows

        deadline = monotonic() + 5
        with (
            patch("archive._BoundedIrsa.query_tap", side_effect=slow_tap) as query,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            first = workers.submit(archive._tap_lookup, self.coord, ["spherex_dr1"],
                                   self.start, self.end, "D1", deadline)
            self.assertTrue(entered.wait(timeout=2))
            second = workers.submit(archive._tap_lookup, self.coord, ["spherex_dr1"],
                                    self.start, self.end, "D4", deadline)
            release.set()
            self.assertEqual(len(first.result(timeout=2)), 1)
            self.assertEqual(len(second.result(timeout=2)), 1)
        self.assertEqual(query.call_count, 1)


if __name__ == "__main__":
    unittest.main()
