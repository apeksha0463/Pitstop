"""
scripts/tests/test_collect_apify_reviews.py
===========================================
Tests for collect_apify_reviews: CID place URLs, converting Apify items to
the review format used everywhere else, matching items to restaurants,
merging re-runs, skipping restaurants that have enough, the spend cap and
the washroom pipeline reading the Apify reviews as unfiltered.

All tests run offline (the Apify API is mocked).
"""

from __future__ import annotations

import csv
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import collect_apify_reviews as apify
    from scripts import extract_washroom_mentions as mentions
    from scripts import utils as _utils
    from scripts.tests.test_washroom import DATA_ID, OTHER_ID, _TempProject
except ImportError:
    import collect_apify_reviews as apify
    import extract_washroom_mentions as mentions
    import utils as _utils
    from tests.test_washroom import DATA_ID, OTHER_ID, _TempProject

CID = str(int("0xCD", 16))           # 205, the CID of DATA_ID "0xAB:0xCD"
REST = {"restaurant": "Test Dhaba", "data_id": DATA_ID, "google_review_count": "900"}
OTHER = {"restaurant": "Other", "data_id": OTHER_ID, "google_review_count": "3"}


def item(rid, text="Nice food", cid=CID, title="Test Dhaba", total=900, **extra):
    return {"reviewId": rid, "cid": cid, "title": title, "reviewsCount": total, "totalScore": 4.2,
            "placeId": "ChIJtest", "address": "Highway 1", "stars": 4, "text": text,
            "publishedAtDate": f"2026-10-{int(rid[1:]) % 28 + 1:02d}T00:00:00.000Z", "publishAt": "a week ago",
            **extra}


class TestConversion(unittest.TestCase):

    def test_place_url_uses_the_cid(self):
        self.assertEqual(apify.place_url("0x3bae19cd83892495:0xc4ee26c182749dfe"),
                         "https://www.google.com/maps?cid=14190322088450235902")

    def test_flatten_item(self):
        review = apify.flatten_item(item("r1", text="Toilet was clean", reviewImageUrls=["https://img/1"],
                                         responseFromOwnerText="Thanks!", likesCount=2))
        self.assertEqual(review["review_id"], "r1")
        self.assertEqual(review["rating"], 4.0)
        self.assertEqual(review["text"], "Toilet was clean")
        self.assertFalse(review["translated"])
        self.assertEqual(review["images"][0]["image_url"], "https://img/1")
        self.assertEqual(review["owner_response"]["text"], "Thanks!")
        self.assertEqual(review["iso_date"], "2026-10-02T00:00:00.000Z")
        self.assertEqual(set(review), set(_utils.flatten_review({"review_id": "x"})))

    def test_translated_text_is_used_for_matching(self):
        review = apify.flatten_item(item("r1", text="ಶೌಚಾಲಯ ಸ್ವಚ್ಛವಾಗಿದೆ", textTranslated="Toilet is clean",
                                         originalLanguage="kn"))
        self.assertEqual(review["text"], "Toilet is clean")
        self.assertEqual(review["text_original_language"], "ಶೌಚಾಲಯ ಸ್ವಚ್ಛವಾಗಿದೆ")
        self.assertTrue(review["translated"])
        self.assertEqual(review["language"], "kn")

    def test_html_line_breaks_and_entities_become_plain_text(self):
        review = apify.flatten_item(item("r1", text="Food good.<br>Washroom dirty &amp; smelly<BR/>Staff ok"))
        self.assertEqual(review["text"], "Food good.\nWashroom dirty & smelly\nStaff ok")

    def test_rating_only_review(self):
        review = apify.flatten_item(item("r1", text="", stars="5"))
        self.assertIsNone(review["text"])
        self.assertEqual(review["rating"], 5.0)

    def test_items_matched_to_restaurants_by_cid(self):
        grouped = apify.group_by_restaurant([item("r1"), item("r2", cid="999"), item("r3")], [REST, OTHER])
        self.assertEqual([i["reviewId"] for i in grouped[DATA_ID]], ["r1", "r3"])
        self.assertNotIn(OTHER_ID, grouped)


class TestSaving(_TempProject):

    def test_rerun_merges_without_duplicates(self):
        apify.save_restaurant(REST, [item("r1"), item("r2")], run_id="run1", requested=2)
        doc = apify.save_restaurant(REST, [item("r2"), item("r3")], run_id="run2", requested=2)
        self.assertEqual(doc["reviews_collected"], 3)
        self.assertEqual(sorted(r["review_id"] for r in doc["reviews"]), ["r1", "r2", "r3"])
        self.assertTrue(doc["restaurant_name_matches_expected"])
        self.assertEqual(doc["total_reviews_reported"], 900)

    def test_wrong_place_is_flagged(self):
        doc = apify.save_restaurant(REST, [item("r1", title="Some Other Place")], run_id=None, requested=1)
        self.assertIs(doc["restaurant_name_matches_expected"], False)

    def test_is_done(self):
        self.assertFalse(apify.is_done(REST, 2))
        apify.save_restaurant(REST, [item("r1"), item("r2")], run_id=None, requested=2)
        self.assertTrue(apify.is_done(REST, 2))
        self.assertFalse(apify.is_done(REST, 3))
        # Only 3 reviews exist on Google: all of them is enough
        apify.save_restaurant(OTHER, [item(f"r{i}", cid=str(0x12), title="Other", total=3) for i in range(3)],
                              run_id=None, requested=100)
        self.assertTrue(apify.is_done(OTHER, 100))

    def test_washroom_pipeline_reads_apify_reviews_as_unfiltered(self):
        apify.save_restaurant(REST, [item("r1", text="Washroom was dirty."), item("r2")], run_id=None, requested=2)
        csv_path = self.root / "restaurants.csv"
        csv_path.write_text("#,Restaurant,data_id,No. of reviews\n1,Test Dhaba,0xAB:0xCD,900\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            mentions.main(["--csv", str(csv_path)])
        with open(_utils.PROCESSED_DIR / "washroom_summary.csv", encoding="utf-8", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual((row["sample_reviews"], row["washroom_in_sample"], row["washroom_share"]),
                         ("2", "1", "0.5"))


class TestRun(_TempProject):

    def test_run_input_and_spend_cap(self):
        calls = []

        def fake_request(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if method == "POST":
                return {"data": {"id": "run1", "status": "READY", "defaultDatasetId": "ds1"}}
            if path.startswith("/actor-runs/"):
                return {"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "ds1"}}
            return [item("r1")] if "offset=0&" in path else []

        with patch.object(apify, "_request", side_effect=fake_request), patch.object(apify.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            run, items = apify.run_actor("TOKEN", [REST], 100, 2.5)
        method, path, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("maxTotalChargeUsd=2.5", path)
        self.assertEqual(body["startUrls"], [{"url": f"https://www.google.com/maps?cid={CID}"}])
        self.assertEqual((body["maxReviews"], body["reviewsSort"], body["personalData"]), (100, "newest", False))
        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertEqual([i["reviewId"] for i in items], ["r1"])

    def test_cap_above_monthly_limit_is_refused(self):
        csv_path = self.root / "restaurants.csv"
        csv_path.write_text("#,Restaurant,data_id\n1,Test Dhaba,0xAB:0xCD\n", encoding="utf-8")
        with patch.object(apify, "load_token", return_value="TOKEN"), \
                patch.object(apify, "monthly_usage", return_value=(4.0, 5.0)), \
                patch.object(apify, "run_actor") as run_actor, redirect_stdout(io.StringIO()):
            code = apify.main(["--csv", str(csv_path), "--max-charge-usd", "2"])
        self.assertEqual(code, 1)
        run_actor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
