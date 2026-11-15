"""Six-band preparation is bounded, prioritizes the chosen detector and stays honest."""

import time
import unittest
from threading import Lock
from unittest.mock import patch

from archive import ArchiveUnavailable
from prepare import PrepareRequest, get_prepare, start_prepare, _jobs, _jobs_by_key


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.request = PrepareRequest(
            ra_deg=12, dec_deg=75, start_date="2025-05-01",
            end_date="2025-10-31", band="D4",
        )
        _jobs.clear()
        _jobs_by_key.clear()
        self.addCleanup(_jobs.clear)
        self.addCleanup(_jobs_by_key.clear)

    def _wait_complete(self, job_id):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = get_prepare(job_id)
            if job and job["status"] == "complete":
                return job
            time.sleep(.01)
        self.fail("Preparation did not reach a terminal state")

    def test_requested_band_first_and_retry_only_unavailable_bands(self):
        calls = []
        mutex = Lock()
        d1_attempts = 0

        def build(selected, *, deadline, refresh=False):
            nonlocal d1_attempts
            with mutex:
                calls.append((selected.band, selected.fov_arcmin, refresh, deadline))
                if selected.band == "D1":
                    d1_attempts += 1
                    if d1_attempts == 1:
                        raise ArchiveUnavailable("The upstream listing is down for D1.")
            return {"status": "ready", "sequence_id": selected.band,
                    "frames": [{"band": selected.band, "source_url": f"https://archive/{selected.band}.fits"}]}

        with (
            patch("prepare._warm_listing") as warm,
            patch("prepare.find_saved_sequence", return_value=None),
            patch("prepare.build_sequence", side_effect=build),
        ):
            started = start_prepare(self.request)
            self.assertEqual(started["canonical_fov_arcmin"], 20)
            self.assertEqual(started["requested_band"], "D4")
            first = self._wait_complete(started["job_id"])
            self.assertEqual(first["completed_count"], 6)
            self.assertEqual(first["bands"]["D1"]["status"], "unavailable")
            self.assertEqual(first["bands"]["D4"]["manifest"]["frames"][0]["band"], "D4")
            self.assertEqual(calls[0][0], "D4")
            self.assertEqual([band for band, _, _, _ in calls].count("D4"), 1)
            self.assertTrue(all(fov == 20 for _, fov, _, _ in calls))
            self.assertTrue(all(deadline > 0 for _, _, _, deadline in calls))

            reused = start_prepare(self.request)
            self.assertEqual(reused["job_id"], first["job_id"])
            self.assertEqual(len(calls), 6)
            retry = start_prepare(self.request, refresh=True)
            self.assertNotEqual(retry["job_id"], first["job_id"])
            second = self._wait_complete(retry["job_id"])
            self.assertEqual(second["bands"]["D1"]["status"], "ready")
            self.assertEqual(second["bands"]["D4"]["manifest"], first["bands"]["D4"]["manifest"])
            self.assertEqual(len(calls), 7)
            self.assertEqual(calls[-1][0], "D1")
            self.assertTrue(calls[-1][2])
            self.assertEqual(warm.call_count, 2)

            # Clients cannot alter the stored manifest by mutating a poll response.
            second["bands"]["D4"]["manifest"]["frames"].clear()
            self.assertEqual(len(get_prepare(retry["job_id"])["bands"]["D4"]["manifest"]["frames"]), 1)

    def test_time_budget_exhaustion_never_marks_unbuilt_bands_as_empty_or_ready(self):
        with (
            patch("prepare._warm_listing"),
            patch("prepare.find_saved_sequence", return_value=None),
            patch("prepare.PREPARE_BUDGET_SECONDS", 0),
            patch("prepare.build_sequence") as build,
        ):
            job = self._wait_complete(start_prepare(self.request)["job_id"])
        build.assert_not_called()
        self.assertEqual(job["completed_count"], 6)
        self.assertEqual({value["status"] for value in job["bands"].values()}, {"unavailable"})
        self.assertTrue(all("budget" in value["error"] for value in job["bands"].values()))

    def test_refresh_rechecks_partial_but_retains_verified_frames_on_failure(self):
        selected_attempts = 0
        def build(selected, *, deadline, refresh=False):
            nonlocal selected_attempts
            if selected.band == "D4":
                selected_attempts += 1
                if selected_attempts == 2:
                    self.assertTrue(refresh)
                    raise ArchiveUnavailable("Index offline during refresh")
                return {"status": "partial", "frames": [{"source_url": "https://archive/genuine-D4.fits"}],
                        "limitations": ["Some dates were missing."]}
            return {"status": "ready", "frames": [{"source_url": "https://archive/other.fits"}]}

        with (
            patch("prepare._warm_listing"),
            patch("prepare.find_saved_sequence", return_value=None),
            patch("prepare.build_sequence", side_effect=build),
        ):
            first = self._wait_complete(start_prepare(self.request)["job_id"])
            refreshed = self._wait_complete(start_prepare(self.request, refresh=True)["job_id"])
        self.assertEqual(selected_attempts, 2)
        self.assertEqual(refreshed["bands"]["D4"]["status"], "partial")
        self.assertEqual(refreshed["bands"]["D4"]["manifest"]["frames"],
                         first["bands"]["D4"]["manifest"]["frames"])
        self.assertIn("Refresh failed", refreshed["bands"]["D4"]["manifest"]["limitations"][0])
        self.assertIn("Index offline", refreshed["bands"]["D4"]["error"])


if __name__ == "__main__":
    unittest.main()
