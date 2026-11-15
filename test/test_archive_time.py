"""UTC bounds sent to IRSA for sequence searches."""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from requests.exceptions import Timeout

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

import config
from api import create_sequence, sequence_cache
from archive import ArchiveUnavailable, _TimedSession, search_observations
from sequence import SequenceRequest, build_sequence, time_spread_indexes


class ArchiveTimeTests(unittest.TestCase):
    def setUp(self):
        # Isolate deliberate service failures from other test cases.
        until = patch("archive._outage_until", 0.0)
        probe = patch("archive._outage_probe", False)
        until.start()
        probe.start()
        self.addCleanup(probe.stop)
        self.addCleanup(until.stop)

    def test_query_sia_receives_utc_interval(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        sentinel = object()

        with patch("archive._BoundedIrsa.query_sia", return_value=sentinel) as query:
            result = search_observations(
                coord, 600, ["spherex_dr1", "spherex_qr3"], start, end
            )

        self.assertIs(result, sentinel)
        self.assertEqual(query.call_args.kwargs["time"], (start, end))
        self.assertEqual(query.call_args.kwargs["pos"][1], 600 * u.arcsec)

    def test_transient_gateway_error_falls_back_to_short_date_windows(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        sentinel = Table(
            [[Time("2025-05-03").mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        empty = sentinel[:0]
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=[
                RuntimeError("502 Bad Gateway"), sentinel, empty, empty, empty,
            ]) as query,
        ):
            result = search_observations(
                coord, 600, ["spherex_dr1"], start, end, band="D3"
            )
        self.assertEqual(len(result), 1)
        self.assertFalse(result.meta.get("archive_listing_partial"))
        self.assertEqual(query.call_count, 5)
        self.assertEqual(query.call_args_list[0].kwargs["time"], (start, end))
        lower, upper = query.call_args.kwargs["band"]
        self.assertAlmostEqual(lower.to_value(u.um), 1.636)
        self.assertAlmostEqual(upper.to_value(u.um), 2.421)

    def test_long_query_fallback_is_marked_incomplete(self):
        coord = SkyCoord(180, -30, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2026-11-15", scale="utc")
        rows = Table(
            [[Time("2025-05-15").mjd], ["SPHEREx-D3"], ["https://example.org/a.fits"]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        with patch("archive._BoundedIrsa.query_sia", side_effect=[
            Timeout("read timed out"), rows, Timeout("read timed out"),
            rows[:0], rows[:0],
        ]) as query:
            result = search_observations(coord, 850, ["spherex_dr1"], start, end, band="D3")
        self.assertEqual(query.call_count, 5)
        self.assertEqual(len(result), 1)
        self.assertTrue(result.meta["archive_listing_partial"])
        self.assertIn("2 of 4", result.meta["archive_listing_window_note"])
        # A slow broad request has not established a global service outage.
        self.assertEqual(__import__("archive")._outage_until, 0.0)

    def test_persistent_archive_failure_has_clear_error(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=RuntimeError("503 Service Unavailable")) as query,
            self.assertRaisesRegex(ArchiveUnavailable, "temporarily unavailable"),
        ):
            search_observations(coord, 600, ["spherex_dr1"], start, end)
        self.assertEqual(query.call_count, 5)

    def test_pyvo_wrapped_read_timeout_is_a_transient_outage(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=RuntimeError(
                "DALFormatError: ReadTimeout: HTTPSConnectionPool: Read timed out."
            )) as query,
            self.assertRaises(ArchiveUnavailable),
        ):
            search_observations(coord, 600, ["spherex_dr1"], start, end)
        self.assertEqual(query.call_count, 5)
        self.assertEqual(__import__("archive")._outage_until, 0.0)

    def test_gateway_failure_does_not_block_other_sky_positions(self):
        coord = SkyCoord(180, -30, unit="deg")
        start, end = Time("2025-07-01"), Time("2025-08-01")
        with (
            patch("archive._BoundedIrsa.query_sia", side_effect=RuntimeError("502 Bad Gateway")) as query,
            self.assertRaises(ArchiveUnavailable),
        ):
            search_observations(coord, 850, ["spherex_dr1"], start, end, band="D3")
        self.assertEqual(query.call_count, 5)
        self.assertEqual(__import__("archive")._outage_until, 0.0)

    def test_confirmed_outage_short_circuits_then_recovers(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        first_listing = object()
        second_listing = object()
        clock = [100.0]
        with (
            patch("archive.monotonic", side_effect=lambda: clock[0]),
            patch("archive._BoundedIrsa.query_sia", side_effect=[
                *[RuntimeError("503 Service Unavailable") for _ in range(5)],
                first_listing,
                second_listing,
            ]) as query,
        ):
            with self.assertRaises(ArchiveUnavailable):
                search_observations(coord, 600, ["spherex_dr1"], start, end)
            with self.assertRaises(ArchiveUnavailable):
                search_observations(coord, 600, ["spherex_dr1"], start, end)
            self.assertEqual(query.call_count, 5)

            clock[0] += 16
            self.assertIs(
                search_observations(coord, 600, ["spherex_dr1"], start, end),
                first_listing,
            )
            self.assertIs(
                search_observations(coord, 600, ["spherex_dr1"], start, end),
                second_listing,
            )
            self.assertEqual(query.call_count, 7)

    def test_only_one_probe_runs_after_outage(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        clock = [100.0]
        calls = [0]
        sentinel = object()

        def query(*_args, **_kwargs):
            calls[0] += 1
            if calls[0] <= 5:
                raise RuntimeError("503 Service Unavailable")
            with self.assertRaises(ArchiveUnavailable):
                search_observations(coord, 600, ["spherex_dr1"], start, end)
            return sentinel

        with (
            patch("archive.monotonic", side_effect=lambda: clock[0]),
            patch("archive._BoundedIrsa.query_sia", side_effect=query),
        ):
            with self.assertRaises(ArchiveUnavailable):
                search_observations(coord, 600, ["spherex_dr1"], start, end)
            clock[0] += 16
            self.assertIs(
                search_observations(coord, 600, ["spherex_dr1"], start, end),
                sentinel,
            )
            self.assertEqual(calls[0], 6)

    def test_sia_session_sets_bounded_network_timeout(self):
        session = _TimedSession()
        session.deadline = 10_000
        with (
            patch("archive.monotonic", return_value=9_990),
            patch("archive.Session.request", return_value=object()) as request,
        ):
            session.request("GET", "https://example.org/SIA")
        self.assertEqual(request.call_args.kwargs["timeout"], 6)

    def test_sequence_search_includes_entire_last_utc_day(self):
        request = SequenceRequest(
            ra_deg=120,
            dec_deg=-85,
            start_date="2022-05-01",
            end_date="2026-11-30",
            fov_arcmin=20,
            band="D3",
        )
        no_results = Table(
            names=["t_min", "energy_bandpassname", "access_url"],
            dtype=[float, "U20", "U20"],
        )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.search_observations", return_value=no_results) as search,
        ):
            manifest = build_sequence(request)

        self.assertEqual(manifest["status"], "empty")
        self.assertEqual(manifest["request"]["ra_deg"], 120)
        self.assertEqual(manifest["request"]["dec_deg"], -85)
        self.assertEqual(search.call_args.args[3].isot, "2025-05-01T00:00:00.000")
        last_day = min(date(2026, 12, 1), datetime.now(timezone.utc).date() + timedelta(days=1))
        self.assertEqual(search.call_args.args[4].isot, f"{last_day.isoformat()}T00:00:00.000")

    def test_dates_before_launch_skip_the_archive(self):
        request = SequenceRequest(
            ra_deg=120, dec_deg=-85, start_date="2022-05-01",
            end_date="2024-12-31", fov_arcmin=5, band="D4",
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.search_observations") as search,
        ):
            manifest = build_sequence(request)
        search.assert_not_called()
        self.assertEqual(manifest["status"], "empty")

    def test_many_exposures_have_a_bounded_time_spread_sample(self):
        times = [Time("2025-05-01").mjd + index * 3 for index in range(102)]
        chosen = time_spread_indexes(times, config.MAX_CUTOUT_ATTEMPTS)
        self.assertEqual(len(chosen), config.MAX_CUTOUT_ATTEMPTS)
        self.assertEqual(len(set(chosen)), config.MAX_CUTOUT_ATTEMPTS)
        self.assertEqual(chosen[0], 0)
        self.assertEqual(chosen[7], 101)

        observations = Table(
            [times, ["SPHEREx-D4"] * 102,
             [f"https://example.org/{index}.fits" for index in range(102)]],
            names=["t_min", "energy_bandpassname", "access_url"],
        )
        request = SequenceRequest(
            ra_deg=120, dec_deg=-85, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=5, band="D4",
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("sequence.config.OUTPUT_ROOT", Path(temporary)),
            patch("sequence.search_observations", return_value=observations),
            patch("sequence.load_cutout", side_effect=OSError("archive 503")) as load,
        ):
            manifest = build_sequence(request)

        self.assertEqual(load.call_count, config.MAX_CUTOUT_ATTEMPTS)
        self.assertEqual(manifest["candidate_count"], 102)
        self.assertEqual(manifest["attempted_count"], config.MAX_CUTOUT_ATTEMPTS)
        self.assertEqual(manifest["skipped_count"], 102 - config.MAX_CUTOUT_ATTEMPTS)
        self.assertTrue(any("Time-spread sample" in note for note in manifest["limitations"]))

    def test_same_request_reuses_completed_sequence(self):
        request = SequenceRequest(
            ra_deg=120, dec_deg=-85, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=5, band="D4",
        )
        sequence_cache.clear()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("api.config.OUTPUT_ROOT", Path(temporary)),
            patch("api.build_sequence", return_value={"status": "ready"}) as build,
        ):
            first = create_sequence(request)
            second = create_sequence(request)
        self.assertIs(first, second)
        build.assert_called_once_with(request)
        sequence_cache.clear()


if __name__ == "__main__":
    unittest.main()
