from collections import OrderedDict
from datetime import date
from threading import Event
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import patch

from astropy.table import Table
from astropy.time import Time

import flipbook


def request(band="D4"):
    return flipbook.FlipbookRequest(
        ra_deg=180, dec_deg=-30, start_date=date(2025, 6, 1),
        end_date=date(2025, 7, 31), band=band,
    )


def candidate(index, band="D4"):
    return {
        "t_min": 60830.0 + index / 20,
        "access_url": (
            "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W26_2A/l2b-v27-2026-199/"
            f"{band[-1]}/level2_2025W26_2A_{index:04d}_1{band}_spx_l2b-v27-2026-199.fits"
        ),
        "energy_bandpassname": f"SPHEREx-{band}",
    }


def wait_for(job_id, condition, timeout=5):
    end = monotonic() + timeout
    while monotonic() < end:
        current = flipbook.get_flipbook(job_id)
        if current and condition(current):
            return current
        sleep(0.01)
    raise AssertionError(f"Flipbook job did not reach the requested state: {flipbook.get_flipbook(job_id)!r}")


class StubWriter:
    blocked = Event()
    release = Event()

    def __init__(self, output_root, request, band):
        self.band = band
        self.frames = {}

    def align(self, row, timeout_seconds):
        if row["access_url"] == candidate(0)["access_url"]:
            self.blocked.set()
            if not self.release.wait(timeout=4):
                raise TimeoutError("test failed to release slow source")
        source_url = row["access_url"]
        return {"source_url": source_url}

    def manifest(self, frames, *, status, **fields):
        for item in frames:
            self.frames[item["source_url"]] = item
        return {**fields, "status": status, "frame_count": len(self.frames),
                "frames": list(self.frames.values())}


class ProgressiveFlipbookTest(TestCase):
    def setUp(self):
        StubWriter.blocked.clear()
        StubWriter.release.clear()
        self.enterContext(patch.multiple(
            flipbook, _jobs=OrderedDict(), _jobs_by_key={}, BandWriter=StubWriter,
        ))
        self.saved = self.enterContext(patch.object(flipbook, "_saved_manifest", return_value=None))

    def test_ready_frames_publish_before_slowest_parent_and_grow_past_eight(self):
        rows = [candidate(i) for i in range(19)]

        def catalog(job, band, deadline, refresh, scheduled_at):
            return ([candidate(0, "D2")] if band == "D2" else rows), False, ()

        with patch.object(flipbook, "_catalog", side_effect=catalog) as catalog_mock:
            job = flipbook.start_flipbook(request())
            job_id = job["job_id"]
            try:
                self.assertTrue(StubWriter.blocked.wait(timeout=3))
                first = wait_for(job_id, lambda snap: bool(snap["bands"]["D4"].get("manifest")))
                self.assertEqual(first["status"], "running")
                self.assertEqual(first["bands"]["D4"]["status"], "running")
                self.assertGreaterEqual(first["bands"]["D4"]["manifest"]["frame_count"], 3)
                self.assertEqual(first["bands"]["D4"]["candidate_count"], 19)
                self.assertEqual(first["bands"]["D2"]["status"], "idle")
                self.assertEqual(flipbook.start_flipbook(request())["job_id"], job_id)
                self.assertEqual(catalog_mock.call_count, 1)
            finally:
                StubWriter.release.set()
                final = wait_for(job_id, lambda snap: snap["status"] == "complete")
            self.assertEqual(final["bands"]["D4"]["status"], "ready")
            self.assertEqual(final["bands"]["D4"]["manifest"]["frame_count"], 19)
            self.assertEqual(final["bands"]["D4"]["attempted_count"], 19)

            selected = flipbook.request_flipbook_band(job_id, "D2")
            self.assertEqual(selected["job_id"], job_id)
            self.assertEqual(selected["requested_band"], "D2")
            final = wait_for(job_id, lambda snap: snap["status"] == "complete")
            self.assertEqual(final["bands"]["D2"]["manifest"]["frame_count"], 1)
            self.assertEqual(final["bands"]["D4"]["manifest"]["frame_count"], 19)
            self.assertEqual(catalog_mock.call_count, 2)

    def test_empty_partial_catalog_is_not_a_verified_empty_band(self):
        with patch.object(flipbook, "_catalog", return_value=([], True, ())):
            job_id = flipbook.start_flipbook(request())["job_id"]
            result = wait_for(job_id, lambda snap: snap["status"] == "complete")
        self.assertEqual(result["bands"]["D4"]["status"], "unavailable")
        self.assertNotIn("manifest", result["bands"]["D4"])

    def test_failed_refresh_keeps_the_previous_real_frames(self):
        StubWriter.release.set()
        with patch.object(flipbook, "_catalog", side_effect=[
            ([candidate(1)], False, ()),
            RuntimeError("Archive offline"),
        ]):
            job_id = flipbook.start_flipbook(request())["job_id"]
            ready = wait_for(job_id, lambda snap: snap["status"] == "complete")
            flipbook.request_flipbook_band(job_id, "D4", refresh=True)
            refreshed = wait_for(job_id, lambda snap: snap["status"] == "complete")
        before, after = ready["bands"]["D4"], refreshed["bands"]["D4"]
        self.assertEqual(after["status"], "partial")
        self.assertEqual(before["manifest"]["frames"], after["manifest"]["frames"])
        self.assertIn("Archive offline", after["manifest"]["limitations"][0])

    def test_candidates_deduplicate_and_keep_exact_detector(self):
        first = Time("2025-06-01", scale="utc").mjd
        table = Table(
            rows=[
                (first + 1, "SPHEREx-D4", candidate(1)["access_url"]),
                (first + 1, "SPHEREx-D4", candidate(1)["access_url"]),
                (first + 2, "SPHEREx-D3", candidate(2, "D3")["access_url"]),
                (first + 2, "SPHEREx-D4", candidate(2, "D3")["access_url"]),
                (first + 3, "SPHEREx-D4", "https://not-irsa.example/image.fits"),
            ],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        job_request = request()
        job = flipbook._Job("unit", (), job_request)
        with patch.object(flipbook, "_shared_catalog", return_value=table):
            rows, partial, notes = flipbook._catalog(job, "D4", monotonic() + 2, False, monotonic())
        self.assertEqual([row["access_url"] for row in rows], [candidate(1)["access_url"]])
        self.assertTrue(partial)
        self.assertIn("verified SPHEREx parent FITS product", notes[0])

    def test_large_plan_enforces_cap_including_first_and_last_exposure(self):
        rows = [candidate(i) for i in range(300)]
        plan = flipbook._candidate_plan(rows)
        self.assertEqual(len(plan), flipbook.MAX_CANDIDATES)
        self.assertEqual(len({item["access_url"] for item in plan}), len(plan))
        self.assertIn(rows[0], plan[:flipbook.CUTOUTS_PER_BAND])
        self.assertIn(rows[-1], plan[:flipbook.CUTOUTS_PER_BAND])

    def test_all_detectors_share_metadata_and_aggregate_real_frame_counts(self):
        first = Time("2025-06-01", scale="utc").mjd
        listing = Table(
            rows=[(first + i, f"SPHEREx-{band}", candidate(i, band)["access_url"])
                  for band in flipbook.BANDS for i in (1, 2)],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        with patch.object(flipbook, "warm_all_band_listing", return_value=listing) as shared:
            job = flipbook.start_flipbook(request("all"))
            self.assertEqual(job["requested_band"], "all")
            result = wait_for(job["job_id"], lambda snap: snap["status"] == "complete")
            self.assertEqual(result["candidate_count"], 12)
            self.assertEqual(result["attempted_count"], 12)
            self.assertEqual(result["frame_count"], 12)
            self.assertEqual(result["completed_count"], 6)
            self.assertTrue(all(entry["status"] == "ready" for entry in result["bands"].values()))
            shared.assert_called_once()

    def test_center_catalog_precedes_fallback_to_fewer_known_sources(self):
        first = Time("2025-06-01", scale="utc").mjd
        listing = Table(
            rows=[(first + i / 10, "SPHEREx-D4", candidate(i)["access_url"]) for i in range(59)],
            names=("t_min", "energy_bandpassname", "access_url"),
        )
        job = flipbook._Job("unit", (), request())
        with patch.object(flipbook, "warm_all_band_listing", side_effect=TimeoutError), \
             patch.object(flipbook, "_query_once", return_value=listing) as center, \
             patch.object(flipbook, "find_saved_sources") as saved:
            rows, partial, notes = flipbook._catalog(job, "D4", monotonic() + 20, False, monotonic())
            self.assertEqual(len(rows), 59)
            self.assertTrue(partial)
            self.assertIn("75 arcseconds", notes[0])
            self.assertEqual(center.call_args.args[1], 75)
            self.assertIsNone(center.call_args.args[5])
            saved.assert_not_called()

    def test_saved_frames_play_during_lookup_and_survive_a_smaller_result(self):
        seed = {
            "status": "partial", "frame_count": 4, "candidate_count": 59,
            "attempted_count": 12, "frames": [candidate(i) for i in range(4)],
        }
        self.saved.side_effect = lambda *args, **kwargs: seed if kwargs.get("legacy_seed") else None
        archive_release = Event()

        def catalog(*args):
            if not archive_release.wait(timeout=4):
                raise TimeoutError("test failed to release archive lookup")
            return [candidate(1)], True, ()

        with patch.object(flipbook, "_catalog", side_effect=catalog):
            job_id = flipbook.start_flipbook(request())["job_id"]
            try:
                running = wait_for(job_id, lambda snap: bool(snap["bands"]["D4"].get("manifest")))
                self.assertEqual(running["bands"]["D4"]["status"], "running")
                self.assertEqual(running["frame_count"], 4)
            finally:
                archive_release.set()
                finished = wait_for(job_id, lambda snap: snap["status"] == "complete")
            self.assertEqual(finished["bands"]["D4"]["manifest"]["frames"], seed["frames"])
            self.assertIn("earlier sequence contains 4", finished["bands"]["D4"]["error"])
