"""Surface archive outages to visitors without implying invalid coordinates."""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import create_sequence, sequence_cache
from archive import ArchiveUnavailable
from sequence import SequenceRequest


class ArchiveErrorTests(unittest.TestCase):
    def test_archive_outage_is_distinct_from_invalid_sky_position(self):
        request = SequenceRequest(
            ra_deg=120, dec_deg=-85, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=5, band="D4",
        )
        sequence_cache.clear()
        self.addCleanup(sequence_cache.clear)
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("api.config.OUTPUT_ROOT", Path(temporary)),
            patch("api.build_sequence", side_effect=ArchiveUnavailable("IRSA is temporarily unavailable.")),
        ):
            with self.assertRaises(HTTPException) as caught:
                create_sequence(request)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("temporarily unavailable", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
