"""Completed real frames remain usable after an API restart."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import create_sequence, sequence_cache
from sequence import SequenceRequest
from sequence_disk_cache import CACHED_DATA_NOTE


class SequenceDiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.request = SequenceRequest(
            ra_deg=120, dec_deg=-85, start_date="2025-05-01",
            end_date="2026-11-15", fov_arcmin=5, band="D4",
        )
        self.sequence_id = "a" * 32
        self.folder = self.root / self.sequence_id
        self.folder.mkdir()
        (self.folder / "frame_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        self.manifest = {
            "sequence_id": self.sequence_id,
            "status": "ready",
            "request": self.request.model_dump(mode="json"),
            "frame_count": 1,
            "frames": [{
                "image_url": f"/outputs/{self.sequence_id}/frame_000.png",
                "source_url": "https://example.org/actual.fits",
            }],
            "limitations": ["Instrument quality flags have not yet been applied."],
        }
        self._write_manifest()
        sequence_cache.clear()
        self.addCleanup(sequence_cache.clear)

    def _write_manifest(self):
        (self.folder / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_exact_real_frames_reopen_without_archive_after_restart(self):
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence") as build,
        ):
            first = create_sequence(self.request)
            sequence_cache.clear()  # A newly started API process.
            second = create_sequence(self.request)
        build.assert_not_called()
        self.assertEqual(first["frames"], second["frames"])
        self.assertEqual(first["limitations"][0], CACHED_DATA_NOTE)
        self.assertEqual(self.manifest["limitations"], [
            "Instrument quality flags have not yet been applied."
        ])

    def test_changed_band_does_not_reuse_different_observations(self):
        changed = self.request.model_copy(update={"band": "D3"})
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            create_sequence(changed)
        build.assert_called_once_with(changed)

    def test_missing_frame_never_returns_broken_manifest(self):
        (self.folder / "frame_000.png").unlink()
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            create_sequence(self.request)
        build.assert_called_once_with(self.request)

    def test_corrupt_manifest_never_blocks_a_fresh_search(self):
        (self.folder / "manifest.json").write_text("{broken", encoding="utf-8")
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            create_sequence(self.request)
        build.assert_called_once_with(self.request)

    def test_empty_result_does_not_prevent_later_real_frames(self):
        (self.folder / "manifest.json").unlink()
        ready = {"status": "ready", "frames": [{"image_url": "real"}]}
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", side_effect=[{"status": "empty"}, ready]) as build,
        ):
            first = create_sequence(self.request)
            second = create_sequence(self.request)
        self.assertEqual(first["status"], "empty")
        self.assertIs(second, ready)
        self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
