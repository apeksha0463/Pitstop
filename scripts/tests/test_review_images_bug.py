"""
scripts/tests/test_review_images_bug.py
=======================================
Regression tests for "0 reviews / 0 review images / Restaurant: G".

Root cause: the data_id 0x3bae183899990949:0x2b57bc21549e67af resolves to a
building floor (place_info = {"title": "G", "type": "Level"}) with
search_information.reviews_results_state = "No reviews", and the run was
reported as a plain "completed" extraction. The fixtures below mirror the
real SerpApi responses saved under data/raw_data/serpapi/ and
data/extracted_pages/ (reviews[].images is a list of URL strings).

No SerpApi credits or network access are used: fetch_page, requests.get and
the image session are mocked.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
_ROOT = _SCRIPTS.parent
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils as _utils
    from scripts import extract_reviews
    from scripts.tests.test_extract_reviews import JPEG, SECRET_KEY, _PipelineBase
except ImportError:
    import utils as _utils
    import extract_reviews
    from tests.test_extract_reviews import JPEG, SECRET_KEY, _PipelineBase

FOOD_FACTORY_ID = "0x3bae19cd83892495:0xc4ee26c182749dfe"

PRADEEP_IMAGES = [
    "https://lh3.googleusercontent.com/grass-cs/photo-a=k-no",
    "https://lh3.googleusercontent.com/grass-cs/photo-b=k-no",
    "https://lh3.googleusercontent.com/grass-cs/photo-c=k-no",
]

PRADEEP_REVIEW = {
    "position": 1,
    "link": "https://www.google.com/maps/reviews/data=!4m8!14m7",
    "rating": 1.0,
    "date": "6 months ago",
    "iso_date": "2026-04-25T13:08:08Z",
    "iso_date_of_last_edit": "2026-04-25T13:08:08Z",
    "images": PRADEEP_IMAGES,
    "source": "Google",
    "review_id": "Ci9DQUlRQUNvZENodHljRjlvT2sxTFlrdGxhRjlUVkhoR01VWmljM2d3WWtkV2QwRRAB",
    "user": {
        "name": "Pradeep Patil",
        "link": "https://www.google.com/maps/contrib/111859952007798744723?hl=en",
        "contributor_id": "111859952007798744723",
        "thumbnail": "https://lh3.googleusercontent.com/a-/avatar=s120",
        "reviews": 6,
        "photos": 3,
    },
    "snippet": "After repeated intimation also, food was without salt and spice.",
    "extracted_snippet": {"original": "After repeated intimation also, food was without salt and spice."},
    "details": {"food": 1, "service": 1, "atmosphere": 3},
    "likes": 0,
}

PLAIN_REVIEW = {
    "review_id": "plain_1",
    "user": {"name": "Asha"},
    "rating": 5.0,
    "date": "a week ago",
    "snippet": "Great dosa.",
}

FOOD_FACTORY_PLACE = {
    "title": "The Food Factory",
    "address": "International Airport Road, Allalasandra, Yelahanka, Bengaluru, Karnataka 560065, India",
    "rating": 4.4,
    "reviews": 3193,
    "type": "Eclectic restaurant",
}

# Exactly what SerpApi returned for the data_id in the bug report
LEVEL_G_RESPONSE = {
    "search_metadata": {"id": "6ae2cd366551b5b17f825596", "status": "Success"},
    "search_parameters": {"engine": "google_maps_reviews",
                          "data_id": "0x3bae183899990949:0x2b57bc21549e67af",
                          "hl": "en", "sort_by": "newestFirst"},
    "place_info": {"title": "G", "type": "Level"},
    "search_information": {"reviews_results_state": "No reviews"},
}


def ff_page(reviews, next_token=None, place=FOOD_FACTORY_PLACE, with_pagination=True):
    page = {"search_metadata": {"status": "Success"}, "place_info": place, "reviews": reviews}
    if with_pagination:
        page["serpapi_pagination"] = {"next_page_token": next_token} if next_token is not None else {}
    return page


class _Pipeline(_PipelineBase):
    """Same temporary project as test_extract_reviews, with real-shaped fixtures."""

    def by_id(self, out):
        return {r["review_id"]: r for r in out["reviews"]}


# 1, 2, 8, 11 -------------------------------------------------------------

class TestReviewWithThreePhotos(_Pipeline):

    def test_valid_response_with_reviews_and_attached_photos(self):
        out = self.run_pages([ff_page([PRADEEP_REVIEW, PLAIN_REVIEW])], ok_urls=PRADEEP_IMAGES)
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual(out["reviews_collected"], 2)
        self.assertEqual(out["reviews_with_images"], 1)
        self.assertEqual(out["images_found"], 3)
        self.assertEqual(out["images_downloaded"], 3)
        self.assertEqual(out["images_download_failed"], 0)
        self.assertEqual(self.by_id(out)["plain_1"]["images"], [])

    def test_three_photos_associated_with_the_right_review(self):
        out = self.run_pages([ff_page([PRADEEP_REVIEW])], ok_urls=PRADEEP_IMAGES)
        review = out["reviews"][0]
        self.assertEqual(review["author"], "Pradeep Patil")
        self.assertEqual(review["images_count"], 3)
        self.assertEqual([i["image_url"] for i in review["images"]], PRADEEP_IMAGES)
        self.assertEqual([i["image_index"] for i in review["images"]], [1, 2, 3])
        for img in review["images"]:
            self.assertEqual(img["download_status"], "downloaded")
            self.assertTrue(img["local_path"].startswith("data/extracted_images/0xAB_0xCD/"))
            self.assertTrue((self.root / img["local_path"]).exists())
        # the reviewer's avatar is not a review photo
        self.assertNotIn(PRADEEP_REVIEW["user"]["thumbnail"], self.session.calls)

    def test_successful_download_writes_real_image_bytes(self):
        out = self.run_pages([ff_page([PRADEEP_REVIEW])], ok_urls=PRADEEP_IMAGES[:1])
        img = out["reviews"][0]["images"][0]
        data = (self.root / img["local_path"]).read_bytes()
        self.assertTrue(data.startswith(JPEG[:3]))
        self.assertTrue(img["local_path"].endswith(".jpg"))
        self.assertEqual(img["http_status"], 200)

    def test_summary_counts_match_reviews(self):
        out = self.run_pages([ff_page([PRADEEP_REVIEW, PLAIN_REVIEW])], ok_urls=PRADEEP_IMAGES[:2])
        saved = json.loads(self.output_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["reviews_collected"], len(saved["reviews"]))
        self.assertEqual(saved["images_found"], sum(len(r["images"]) for r in saved["reviews"]))
        self.assertEqual(saved["images_downloaded"], 2)
        self.assertEqual(saved["images_download_failed"], 1)
        self.assertEqual(saved["pages_fetched"], 1)
        self.assertEqual(saved["total_reviews_reported"], 3193)
        self.assertIn("Review images downloaded      : 2", self.stdout)


# 9 -----------------------------------------------------------------------

class TestFailedDownloads(_Pipeline):

    def test_forbidden_and_timeout_recorded_per_image(self):
        session = MagicMock()
        session.headers = {}

        def get(url, **kw):
            if url.endswith("photo-b=k-no"):
                raise _utils.requests.exceptions.Timeout()
            resp = MagicMock(status_code=403, headers={"Content-Type": "text/html"})
            return resp

        session.get.side_effect = get
        try:
            from scripts import extract_review_images as eri
        except ImportError:
            import extract_review_images as eri
        dl = eri.ReviewImageDownloader("0xAB:0xCD", session=session, delay=0, max_retries=1)
        with patch.object(extract_reviews, "fetch_page", return_value=ff_page([PRADEEP_REVIEW])), \
             patch("sys.stdout"):
            out = extract_reviews.run_extraction(SECRET_KEY, "0xAB:0xCD", delay=0, downloader=dl)
        imgs = out["reviews"][0]["images"]
        self.assertEqual([i["download_status"] for i in imgs], ["failed", "failed", "failed"])
        self.assertEqual(imgs[0]["http_status"], 403)
        self.assertEqual(imgs[1]["error"], "Timeout")
        self.assertTrue(all(i["local_path"] is None for i in imgs))
        self.assertEqual(out["images_found"], 3)
        self.assertEqual(out["images_downloaded"], 0)
        self.assertEqual(out["images_download_failed"], 3)
        # URLs are kept so they can be retried later
        self.assertEqual([i["image_url"] for i in imgs], PRADEEP_IMAGES)
        self.assertEqual(out["collection_status"], "completed")


# 3, 10 -------------------------------------------------------------------

class TestWrongPlaceAndEmptyReviews(_Pipeline):

    def test_level_g_response_is_not_reported_as_plain_completed(self):
        out = self.run_pages([LEVEL_G_RESPONSE])
        self.assertEqual(out["collection_status"], "completed_with_no_reviews")
        self.assertEqual(out["reviews_collected"], 0)
        self.assertEqual(out["restaurant_name"], "G")     # verbatim, not truncated
        self.assertEqual(out["type"], "Level")
        self.assertEqual(out["reviews_results_state"], "No reviews")
        self.assertIn("floor/level", out["place_warning"])
        self.assertIn("WARNING", self.stdout)
        self.assertIn("[DIAG]", self.stdout)                # diagnostics shown automatically

    def test_expected_name_mismatch_flagged(self):
        seq = [LEVEL_G_RESPONSE]
        with patch.object(extract_reviews, "fetch_page", side_effect=seq), patch("sys.stdout"):
            out = extract_reviews.run_extraction(SECRET_KEY, "0xAB:0xCD", delay=0,
                                                 expected_name="The Food Factory")
        self.assertIs(out["restaurant_name_matches_expected"], False)

    def test_empty_reviews_array_on_real_restaurant(self):
        out = self.run_pages([ff_page([])])
        self.assertEqual(out["collection_status"], "completed_with_no_reviews")
        self.assertIsNone(out["place_warning"])
        self.assertEqual(out["reviews"], [])

    def test_restaurant_name_from_place_info_title(self):
        info = _utils.extract_place_info(ff_page([]))
        self.assertEqual(info["restaurant_name"], "The Food Factory")
        self.assertEqual(info["name_source"], "place_info.title")
        self.assertIsNone(info["place_warning"])
        self.assertTrue(_utils.name_matches(info["restaurant_name"], "the food factory"))

    def test_missing_place_info_is_flagged_unverified(self):
        info = _utils.extract_place_info({"reviews": []})
        self.assertEqual(info["restaurant_name"], "Unknown")
        self.assertIsNone(info["name_source"])
        self.assertIn("NOT verified", info["place_warning"])


# 4 -----------------------------------------------------------------------

def _http_response(status, body):
    resp = MagicMock(status_code=status)
    resp.json.return_value = body
    return resp


class TestApiErrorResponses(_Pipeline):

    def test_error_body_raises_and_records_http_status(self):
        meta = {}
        with patch.object(_utils.requests, "get",
                          return_value=_http_response(200, {"error": "Invalid data_id parameter."})):
            with self.assertRaises(_utils.SerpApiError) as ctx:
                _utils.fetch_page(SECRET_KEY, {"engine": "google_maps_reviews"}, meta=meta)
        self.assertIn("Invalid data_id", str(ctx.exception))
        self.assertEqual(meta["http_status"], 200)

    def test_search_metadata_error_status_raises(self):
        body = {"search_metadata": {"status": "Error"}, "reviews": []}
        with patch.object(_utils.requests, "get", return_value=_http_response(200, body)):
            with self.assertRaises(_utils.SerpApiError):
                _utils.fetch_page(SECRET_KEY, {})

    def test_quota_error_raises_credits_exhausted(self):
        body = {"error": "Your account has run out of searches."}
        with patch.object(_utils.requests, "get", return_value=_http_response(200, body)):
            with self.assertRaises(_utils.CreditsExhaustedError):
                _utils.fetch_page(SECRET_KEY, {})

    def test_no_results_message_kept_in_meta(self):
        meta = {}
        body = {"error": "Google hasn't returned any results for this query."}
        with patch.object(_utils.requests, "get", return_value=_http_response(200, body)):
            data = _utils.fetch_page(SECRET_KEY, {}, meta=meta)
        self.assertEqual(data["reviews"], [])
        self.assertIn("hasn't returned", meta["api_message"])

    def test_api_error_on_first_page_is_saved_not_completed(self):
        out = self.run_pages([_utils.SerpApiError(f"SerpApi API error: bad id api_key={SECRET_KEY}")])
        self.assertEqual(out["collection_status"], "api_error")
        self.assertFalse(out["collection_complete"])
        self.assertEqual(out["api_error"]["type"], "api_error")
        self.assertEqual(out["api_error"]["page"], 1)
        self.assertNotIn(SECRET_KEY, self.all_written_text())
        self.assertNotIn(SECRET_KEY, self.stdout)
        self.assertEqual(extract_reviews.exit_code(out), 1)

    def test_quota_exhausted_on_first_page(self):
        out = self.run_pages([_utils.CreditsExhaustedError("No searches left.")])
        self.assertEqual(out["collection_status"], "quota_exhausted")
        self.assertEqual(extract_reviews.exit_code(out), 1)


# 5, 6 --------------------------------------------------------------------

class TestPaginationEdgeCases(_Pipeline):

    def test_missing_serpapi_pagination_ends_cleanly(self):
        out = self.run_pages([ff_page([PLAIN_REVIEW], with_pagination=False)])
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(out["page_diagnostics"][0]["has_serpapi_pagination"])

    def test_empty_string_token_treated_as_no_token(self):
        out = self.run_pages([ff_page([PLAIN_REVIEW], next_token="")])
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_token_error_keeps_partial_results(self):
        pages = [ff_page([PRADEEP_REVIEW], next_token="BROKEN"),
                 _utils.SerpApiError("Bad request (HTTP 400): Invalid next_page_token.")]
        out = self.run_pages(pages, ok_urls=PRADEEP_IMAGES)
        self.assertEqual(out["collection_status"], "partial")
        self.assertEqual(out["reviews_collected"], 1)
        self.assertEqual(out["images_downloaded"], 3)
        self.assertEqual(out["api_error"]["page"], 2)
        self.assertEqual(self.calls[1]["next_page_token"], "BROKEN")
        self.assertEqual(extract_reviews.exit_code(out), 0)

    def test_multiple_pages_deduplicated(self):
        p1 = ff_page([PRADEEP_REVIEW], next_token="t2")
        p2 = ff_page([PLAIN_REVIEW, PRADEEP_REVIEW], next_token="t3")
        p3 = ff_page([{**PLAIN_REVIEW, "review_id": "plain_2"}])
        out = self.run_pages([p1, p2, p3], ok_urls=PRADEEP_IMAGES)
        self.assertEqual(out["pages_fetched"], 3)
        self.assertEqual(out["reviews_collected"], 3)
        self.assertEqual(out["duplicates_skipped"], 1)
        self.assertEqual(out["images_found"], 3)
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual([c.get("next_page_token") for c in self.calls], [None, "t2", "t3"])

    def test_sort_by_passed_to_api(self):
        with patch.object(extract_reviews, "fetch_page", return_value=ff_page([])) as fp, patch("sys.stdout"):
            out = extract_reviews.run_extraction(SECRET_KEY, "0xAB:0xCD", delay=0, sort_by="qualityScore")
        self.assertEqual(fp.call_args.args[1]["sort_by"], "qualityScore")
        self.assertEqual(out["sort_by"], "qualityScore")


# 7 -----------------------------------------------------------------------

class TestMissingOptionalFields(_Pipeline):

    def test_minimal_review_does_not_crash(self):
        out = self.run_pages([ff_page([{"review_id": "bare"}, {"snippet": "No id, no user"}])])
        self.assertEqual(out["reviews_collected"], 2)
        bare = self.by_id(out)["bare"]
        self.assertIsNone(bare["author"])
        self.assertIsNone(bare["rating"])
        self.assertIsNone(bare["text"])
        self.assertEqual(bare["images"], [])

    def test_junk_image_entries_dropped_object_entries_supported(self):
        review = {"review_id": "mixed", "images": [None, "", "not-a-url",
                                                   {"thumbnail": "https://img.test/obj"},
                                                   "https://img.test/str"]}
        imgs = _utils.flatten_review(review)["images"]
        self.assertEqual([i["image_url"] for i in imgs], ["https://img.test/obj", "https://img.test/str"])


# Diagnostics -------------------------------------------------------------

class TestDescribeResponse(unittest.TestCase):

    def test_reports_structure_without_urls(self):
        d = _utils.describe_response(ff_page([PRADEEP_REVIEW], next_token="tok"), 200)
        self.assertEqual(d["http_status"], 200)
        self.assertEqual(d["reviews_in_response"], 1)
        self.assertTrue(d["has_next_page_token"])
        self.assertTrue(d["first_review_has_images"])
        self.assertEqual(d["first_review_image_count"], 3)
        self.assertEqual(d["first_review_image_entry_types"], ["str"])
        self.assertIn("images", d["first_review_keys"])
        self.assertNotIn("googleusercontent", json.dumps(d))

    def test_level_g_response(self):
        d = _utils.describe_response(LEVEL_G_RESPONSE, 200)
        self.assertEqual(d["reviews_in_response"], 0)
        self.assertEqual(d["place_type"], "Level")
        self.assertEqual(d["reviews_results_state"], "No reviews")
        self.assertFalse(d["has_serpapi_pagination"])
        self.assertIsNone(d["first_review_keys"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
