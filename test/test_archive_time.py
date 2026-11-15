"""UTC bounds sent to IRSA for sequence searches."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.table import Table
from astropy.time import Time

from archive import search_observations
from sequence import SequenceRequest, build_sequence


class ArchiveTimeTests(unittest.TestCase):
    def test_query_sia_receives_utc_interval(self):
        coord = SkyCoord(120, -85, unit="deg")
        start = Time("2025-05-01", scale="utc")
        end = Time("2025-06-01", scale="utc")
        sentinel = object()

        with patch("archive.Irsa.query_sia", return_value=sentinel) as query:
            result = search_observations(
                coord, 600, ["spherex_dr1", "spherex_qr3"], start, end
            )

        self.assertIs(result, sentinel)
        self.assertEqual(query.call_args.kwargs["time"], (start, end))
        self.assertEqual(query.call_args.kwargs["pos"][1], 600 * u.arcsec)

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
        self.assertEqual(search.call_args.args[3].isot, "2022-05-01T00:00:00.000")
        self.assertEqual(search.call_args.args[4].isot, "2026-12-01T00:00:00.000")


if __name__ == "__main__":
    unittest.main()
