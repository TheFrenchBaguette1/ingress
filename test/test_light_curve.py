import unittest
from collections import OrderedDict, deque
from unittest.mock import patch

from fastapi import HTTPException

import api
import light_curve as lc
from flipbook import FlipbookRequest
from photometry_inputs import PhotometryInputUnavailable


PARENT = "https://irsa.ipac.caltech.edu/ibe/data/spherex/{release}/level2/w/v/4/level2_w_{n:04d}_2D4_spx_{release}.fits"
SEARCH = FlipbookRequest(ra_deg=248.1675, dec_deg=66.755, start_date="2025-08-26", end_date="2025-08-29", band="all")


def exposure(n, mjd=60914.25, release="dr1"):
    return {"source_url": PARENT.format(n=n, release=release), "time_mjd": mjd, "band": "D4"}


def measurement(parent, ra, dec):
    return {
        "product_id": parent.product_id, "time_mjd": parent.mjd, "time_utc": "2025-08-27T06:00:00Z",
        "detector": "D4", "channel": 1, "wavelength_um": 2.5, "bandwidth_um": 0.04,
        "channel_wavelength_um": 2.5, "channel_bandwidth_um": 0.04,
        "flux_ujy": 100, "stat_error_ujy": 1, "error_ujy": 3.16,
    }


class LightCurveTest(unittest.TestCase):
    def setUp(self):
        self.tasks = deque()
        replacements = patch.multiple(lc, _curves=OrderedDict(), _curve_ids={}, _pending=OrderedDict(),
                                      _measurements=OrderedDict(), _inflight={}, _running=0)
        replacements.start()
        self.addCleanup(replacements.stop)
        executor = patch.object(lc._executor, "submit", side_effect=lambda run, *args: self.tasks.append((run, args)))
        executor.start()
        self.addCleanup(executor.stop)
        fit = patch.object(lc, "_measure", side_effect=measurement)
        self.measure = fit.start()
        self.addCleanup(fit.stop)

    def run_next(self):
        run, args = self.tasks.popleft()
        run(*args)

    def test_snapshot_filters_dates_and_prefers_one_dr1_parent_per_exposure(self):
        frames = [exposure(1, release="qr3"), exposure(1), exposure(2, mjd=60917.0),
                  {**exposure(3), "source_url": "https://example.com/product.fits"}]
        query = lc.LightCurveRequest(start_date="2025-08-27", end_date="2025-08-27")
        job = lc.start_light_curve(SEARCH, frames, query)
        self.assertEqual(job["total_count"], 3)
        self.assertEqual(job["processed_count"], 2)
        self.assertEqual(job["exclusions"], {"duplicate_observation": 1, "unsupported_product": 1})
        self.run_next()
        result = lc.get_light_curve(job["light_curve_id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["points"][0]["product_id"], "level2_w_0001_2D4_spx_dr1.fits")
        self.assertEqual(result["channels"][0]["day_count"], 1)

    def test_overlapping_snapshots_share_inflight_fits_reads_and_cached_measurements(self):
        first = lc.start_light_curve(SEARCH, [exposure(1)], lc.LightCurveRequest())
        second = lc.start_light_curve(SEARCH, [exposure(1), exposure(2)], lc.LightCurveRequest())
        self.assertEqual(len(self.tasks), 2)
        self.assertEqual(lc.start_light_curve(SEARCH, [exposure(1)], lc.LightCurveRequest()), first)
        self.run_next()
        self.assertEqual(lc.get_light_curve(first["light_curve_id"])["status"], "complete")
        self.assertEqual(lc.get_light_curve(second["light_curve_id"])["processed_count"], 1)
        self.run_next()
        cached = lc.start_light_curve(SEARCH, [exposure(2)], lc.LightCurveRequest())
        self.assertEqual(cached["status"], "complete")
        self.assertEqual(self.measure.call_count, 2)

    def test_transient_exclusions_can_be_retried(self):
        first = lc.start_light_curve(SEARCH, [exposure(1)], lc.LightCurveRequest())
        self.measure.side_effect = PhotometryInputUnavailable("archive_unavailable", "Temporary failure")
        self.run_next()
        self.assertEqual(lc.get_light_curve(first["light_curve_id"])["exclusions"], {"archive_unavailable": 1})
        second = lc.start_light_curve(SEARCH, [exposure(1)], lc.LightCurveRequest())
        self.assertNotEqual(first["light_curve_id"], second["light_curve_id"])
        self.measure.side_effect = measurement
        self.run_next()
        self.assertEqual(len(lc.get_light_curve(second["light_curve_id"])["points"]), 1)

    def test_off_center_selection_stays_on_the_loaded_products(self):
        query = lc.LightCurveRequest(ra_deg=248.11, dec_deg=66.8)
        job = lc.start_light_curve(SEARCH, [exposure(1)], query)
        self.assertEqual(job["target"], {"ra_deg": 248.11, "dec_deg": 66.8})
        self.run_next()
        parent, ra, dec = self.measure.call_args.args
        self.assertEqual((parent.url, ra, dec), (exposure(1)["source_url"], 248.11, 66.8))
        with self.assertRaisesRegex(ValueError, "outside the loaded sky patch"):
            lc.start_light_curve(SEARCH, [exposure(1)], lc.LightCurveRequest(ra_deg=250, dec_deg=68))
        with self.assertRaisesRegex(ValueError, "both coordinates"):
            lc.LightCurveRequest(ra_deg=248.11)

    def test_create_route_accepts_only_known_job_and_returns_capacity_signal(self):
        with patch.object(api, "get_flipbook_exposures", return_value=(SEARCH, (exposure(1),))) as resolve:
            with self.assertRaises(HTTPException) as unknown:
                api.create_light_curve("bad-job", lc.LightCurveRequest())
            self.assertEqual(unknown.exception.status_code, 404)
            resolve.assert_not_called()
            with patch.object(api, "start_light_curve", side_effect=lc.LightCurveBusy("Queue is full")):
                with self.assertRaises(HTTPException) as busy:
                    api.create_light_curve("a" * 32, lc.LightCurveRequest())
        self.assertEqual(busy.exception.status_code, 503)
        self.assertEqual(busy.exception.headers["Retry-After"], "3")
        self.assertEqual(busy.exception.detail["code"], "light_curve_busy")


if __name__ == "__main__":
    unittest.main()
