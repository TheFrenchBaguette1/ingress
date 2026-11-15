"""Completed real frames remain usable after an API restart."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from api import create_sequence, sequence_cache
from sequence import SequenceRequest
from sequence_disk_cache import CACHED_DATA_NOTE, CACHED_PARTIAL_NOTE, PARTIAL_TTL_SECONDS


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
        Image.new("RGBA", (2, 2), (255, 255, 255, 255)).save(
            self.folder / "frame_000.png"
        )
        self.manifest = {
            "sequence_id": self.sequence_id,
            "status": "ready",
            "request": self.request.model_dump(mode="json"),
            "frame_count": 1,
            "frames": [{
                "image_url": f"/outputs/{self.sequence_id}/frame_000.png",
                "source_url": "https://example.org/actual.fits",
                "coverage_fraction": 0.92,
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

    def test_removed_frame_invalidates_the_in_memory_sequence(self):
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            self.assertEqual(create_sequence(self.request)["status"], "ready")
            (self.folder / "frame_000.png").unlink()
            self.assertEqual(create_sequence(self.request)["status"], "empty")
        build.assert_called_once_with(self.request)

    def test_corrupt_png_does_not_count_as_an_intact_frame(self):
        (self.folder / "frame_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            self.assertEqual(create_sequence(self.request)["status"], "empty")
        build.assert_called_once_with(self.request)

    def test_old_edge_only_image_is_not_reused(self):
        self.manifest["frames"][0]["coverage_fraction"] = 0.04
        self._write_manifest()
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            self.assertEqual(create_sequence(self.request)["status"], "empty")
        build.assert_called_once_with(self.request)

    def test_saved_image_with_blank_target_center_is_not_reused(self):
        Image.new("RGBA", (4, 4), (255, 255, 255, 0)).save(
            self.folder / "frame_000.png"
        )
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            self.assertEqual(create_sequence(self.request)["status"], "empty")
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

    def test_partial_real_frames_reopen_quickly_and_are_labeled(self):
        self.manifest["status"] = "partial"
        self.manifest["archive_listing_partial"] = True
        self._write_manifest()
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence") as build,
        ):
            first = create_sequence(self.request)
            warm = create_sequence(self.request)  # Memory reuse avoids a disk scan.
            sequence_cache.clear()  # Disk reuse also works after restart.
            second = create_sequence(self.request)
        build.assert_not_called()
        self.assertTrue(first["cached_partial"])
        self.assertTrue(warm["cached_partial"])
        self.assertTrue(second["cached_partial"])
        self.assertIn(CACHED_PARTIAL_NOTE, first["limitations"])
        self.assertEqual(second["cache_ttl_seconds"], PARTIAL_TTL_SECONDS)
        self.assertNotIn("cached_partial", self.manifest)

    def test_refresh_bypasses_both_sequence_caches(self):
        self.manifest["status"] = "partial"
        self.manifest["failures"] = [{"source_url": "https://example.org/b.fits"}]
        self._write_manifest()
        refreshed = {"status": "empty", "frames": []}
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value=refreshed) as build,
        ):
            self.assertTrue(create_sequence(self.request)["cached_partial"])
            self.assertIs(create_sequence(self.request, refresh=True), refreshed)
        build.assert_called_once_with(self.request, refresh=True)

    def test_partial_result_expires_and_tries_archive_again(self):
        self.manifest["status"] = "partial"
        self.manifest["archive_listing_partial"] = True
        self._write_manifest()
        old = time.time() - PARTIAL_TTL_SECONDS - 10
        os.utime(self.folder / "manifest.json", (old, old))
        with (
            patch("api.config.OUTPUT_ROOT", self.root),
            patch("api.build_sequence", return_value={"status": "empty"}) as build,
        ):
            self.assertEqual(create_sequence(self.request)["status"], "empty")
        build.assert_called_once_with(self.request)


if __name__ == "__main__":
    unittest.main()
