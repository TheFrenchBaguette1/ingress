import tempfile
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from astropy.coordinates import SkyCoord
from astropy.time import Time

import henry_catalog as catalog


def parent(release="dr1", exposure="0001", detector=4):
    return (
        f"https://irsa.ipac.caltech.edu/ibe/data/spherex/{release}/level2/"
        f"2025W22_1B/l2b-v27-2026-199/{detector}/"
        f"level2_2025W22_1B_{exposure}_1D{detector}_spx_l2b-v27-2026-199.fits"
    )


class HenryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        output_patch = patch.object(catalog.config, "OUTPUT_ROOT", Path(self.cache.name))
        output_patch.start()
        self.addCleanup(output_patch.stop)
        self.coord = SkyCoord(25, -15, unit="deg")
        self.first = Time(60820, format="mjd")
        self.last = Time(60825, format="mjd")

    def search(self, refresh=False):
        return catalog.search_catalog(self.coord, self.first, self.last,
                                      deadline=monotonic() + 1, refresh=refresh)

    def test_csv_requires_complete_rows_and_matching_parent(self):
        header = "s_ra,t_min,energy_bandpassname,access_url,dataproduct_type\n"
        valid = f"25,60821.125,SPHEREx-D4,{parent()},image\n"
        text = header + valid + f"25,60823,SPHEREx-D3,{parent()},image\n25,60822,SPHEREx-D4,"
        parsed, complete = catalog._parse_csv(text, "spherex_dr1", 60820, 60825)
        self.assertEqual(parsed, [(60821.125, "SPHEREx-D4", parent())])
        self.assertFalse(complete)
        self.assertEqual(catalog._parse_csv(header, "spherex_dr1", 60820, 60825), ([], True))
        self.assertIsNone(catalog._parse_csv('"bad header', "spherex_dr1", 60820, 60825))

    def test_collection_results_keep_canonical_parent_and_use_fresh_cache(self):
        def download(collection, *_):
            if collection == "spherex_dr1":
                return [(60821, "SPHEREx-D4", parent())], True
            if collection == "spherex_qr3":
                return [(60821, "SPHEREx-D4", parent("qr3")),
                        (60822, "SPHEREx-D4", parent("qr3", "0002"))], True
            return [], True

        with patch.object(catalog, "_download", side_effect=download) as fetch:
            fresh = self.search()
            cached = self.search()
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(list(fresh["access_url"]), [parent(), parent("qr3", "0002")])
        self.assertFalse(fresh.meta["archive_listing_partial"])
        self.assertFalse(fresh.meta["archive_listing_cached"])
        self.assertTrue(cached.meta["archive_listing_cached"])

    def test_stale_snapshot_and_new_partial_rows_both_survive_refresh(self):
        def initial(collection, *_):
            if collection == "spherex_dr1":
                return [(60821, "SPHEREx-D4", parent())], True
            return None

        with patch.object(catalog, "_download", side_effect=initial):
            fresh = self.search()
        self.assertTrue(fresh.meta["archive_listing_partial"])
        self.assertFalse(fresh.meta["archive_listing_stale"])

        def partial(collection, *_):
            if collection == "spherex_dr1":
                return [(60822, "SPHEREx-D4", parent(exposure="0002"))], False
            return None

        with patch.object(catalog, "_download", side_effect=partial):
            refreshed = self.search(refresh=True)
        self.assertEqual(list(refreshed["access_url"]), [parent(), parent(exposure="0002")])
        self.assertTrue(refreshed.meta["archive_listing_partial"])
        self.assertTrue(refreshed.meta["archive_listing_stale"])
        self.assertEqual(refreshed.meta["archive_listing_collections"]["spherex_dr1"], "stale")


if __name__ == "__main__":
    unittest.main()
