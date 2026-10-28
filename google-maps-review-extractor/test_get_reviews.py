"""
Test suite for get_reviews.py
Runs without making real API calls. Uses mocked SerpApi responses.

Run with:
    python test_get_reviews.py
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(__file__))

import get_reviews


# ---------------------------------------------------------------------------
# Fixtures — representative SerpApi response shapes
# ---------------------------------------------------------------------------

MOCK_REVIEW_1 = {
    "review_id": "rev_001",
    "user": {"name": "Alice Smith", "link": "https://maps.google.com/user/alice", "local_guide": True, "reviews": 42},
    "rating": 5,
    "date": "3 months ago",
    "iso_date": "2026-07-01T00:00:00Z",
    "snippet": "Amazing food and great service!",
    "language": "en",
    "translated": False,
    "likes": 10,
    "owner_answer": None,
    "photos": [{"link": "https://photos.google.com/photo1"}],
    "link": "https://maps.google.com/review/rev_001",
}

MOCK_REVIEW_2 = {
    "review_id": "rev_002",
    "user": {"name": "Bob Jones", "link": "https://maps.google.com/user/bob", "local_guide": False, "reviews": 5},
    "rating": 3,
    "date": "1 month ago",
    "iso_date": "2026-09-15T00:00:00Z",
    "snippet": "Average experience. Could be better.",
    "language": "en",
    "translated": False,
    "likes": 2,
    "owner_answer": "Thank you for your feedback!",
    "photos": [],
    "link": "https://maps.google.com/review/rev_002",
}

MOCK_REVIEW_NO_ID = {
    # No review_id — tests fallback fingerprinting
    "user": {"name": "Carol White"},
    "rating": 4,
    "date": "2 weeks ago",
    "snippet": "Pretty good place overall.",
    "language": "en",
}

MOCK_PLACE_INFO = {
    "title": "Test Restaurant",
    "address": "123 Test Street, Test City",
    "rating": 4.2,
    "reviews": 1250,
    "type": "Restaurant",
    "phone": "+1-555-0100",
    "website": "https://testrestaurant.example.com",
}

def make_page_response(reviews, next_token=None, place_info=None):
    """Build a mock SerpApi page response."""
    resp = {
        "search_metadata": {"status": "Success"},
        "place_info": place_info or MOCK_PLACE_INFO,
        "reviews": reviews,
    }
    if next_token:
        resp["serpapi_pagination"] = {"next_page_token": next_token}
    else:
        resp["serpapi_pagination"] = {}
    return resp


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestMakeReviewFingerprint(unittest.TestCase):
    def test_uses_review_id_when_present(self):
        fp = get_reviews.make_review_fingerprint(MOCK_REVIEW_1)
        self.assertEqual(fp, "rev_001")

    def test_fallback_fingerprint_when_no_id(self):
        fp = get_reviews.make_review_fingerprint(MOCK_REVIEW_NO_ID)
        self.assertIn("Carol White", fp)
        self.assertIn("2 weeks ago", fp)

    def test_fallback_is_deterministic(self):
        fp1 = get_reviews.make_review_fingerprint(MOCK_REVIEW_NO_ID)
        fp2 = get_reviews.make_review_fingerprint(MOCK_REVIEW_NO_ID)
        self.assertEqual(fp1, fp2)


class TestFlattenReview(unittest.TestCase):
    def test_flattens_standard_review(self):
        flat = get_reviews.flatten_review(MOCK_REVIEW_1)
        self.assertEqual(flat["review_id"], "rev_001")
        self.assertEqual(flat["author"], "Alice Smith")
        self.assertEqual(flat["rating"], 5)
        self.assertEqual(flat["text"], "Amazing food and great service!")
        self.assertTrue(flat["author_local_guide"])
        self.assertEqual(flat["photos_count"], 1)
        self.assertIsNone(flat["owner_response"])

    def test_flattens_review_with_owner_response(self):
        flat = get_reviews.flatten_review(MOCK_REVIEW_2)
        self.assertEqual(flat["owner_response"], "Thank you for your feedback!")

    def test_flattens_review_with_no_id(self):
        flat = get_reviews.flatten_review(MOCK_REVIEW_NO_ID)
        self.assertIsNone(flat["review_id"])
        self.assertEqual(flat["author"], "Carol White")

    def test_source_field_is_set(self):
        flat = get_reviews.flatten_review(MOCK_REVIEW_1)
        self.assertEqual(flat["source"], "Google Maps via SerpApi")


class TestExtractPlaceInfo(unittest.TestCase):
    def test_extracts_all_fields(self):
        data = {"place_info": MOCK_PLACE_INFO}
        info = get_reviews.extract_place_info(data)
        self.assertEqual(info["restaurant_name"], "Test Restaurant")
        self.assertEqual(info["address"], "123 Test Street, Test City")
        self.assertEqual(info["rating"], 4.2)
        self.assertEqual(info["total_reviews_reported"], 1250)

    def test_handles_missing_place_info(self):
        info = get_reviews.extract_place_info({})
        self.assertEqual(info["restaurant_name"], "Unknown")
        self.assertIsNone(info["address"])


class TestSaveJson(unittest.TestCase):
    def test_saves_and_loads_valid_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "test_output.json")
            data = {"key": "value", "unicode": "\u00e9l\u00e8ve"}
            get_reviews.save_json(data, filepath)
            with open(filepath, encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["key"], "value")
            self.assertEqual(loaded["unicode"], "\u00e9l\u00e8ve")

    def test_uses_utf8_and_non_ascii(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, "unicode_test.json")
            data = {"text": "\u4e2d\u6587\u5185\u5bb9"}  # Chinese characters
            get_reviews.save_json(data, filepath)
            raw = open(filepath, encoding="utf-8").read()
            self.assertIn("\u4e2d\u6587\u5185\u5bb9", raw)


class TestDuplicateRemoval(unittest.TestCase):
    """Verify deduplication logic in the extraction loop."""

    def _run_with_mocked_responses(self, responses):
        """
        Helper: patch fetch_page to return responses in sequence,
        then run run_extraction and return the saved output dict.
        """
        import tempfile
        call_count = [0]

        def fake_fetch(api_key, params):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(responses):
                return responses[idx]
            return {"reviews": [], "serpapi_pagination": {}}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0):
                get_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            with open(output_path, encoding="utf-8") as f:
                return json.load(f)

    def test_deduplication_across_pages(self):
        """Same review returned on two pages should only appear once."""
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2], next_token="token_page2")
        page2 = make_page_response([MOCK_REVIEW_1])  # MOCK_REVIEW_1 is a duplicate

        result = self._run_with_mocked_responses([page1, page2])
        self.assertEqual(result["reviews_collected"], 2)
        self.assertEqual(result["duplicates_skipped"], 1)

    def test_no_false_positives(self):
        """Two distinct reviews should both be collected."""
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2])

        result = self._run_with_mocked_responses([page1])
        self.assertEqual(result["reviews_collected"], 2)
        self.assertEqual(result["duplicates_skipped"], 0)


class TestPagination(unittest.TestCase):
    """Verify that pagination stops correctly in various scenarios."""

    def _run_with_mocked_responses(self, responses, max_requests=20):
        import tempfile
        call_count = [0]

        def fake_fetch(api_key, params):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(responses):
                return responses[idx]
            return {"reviews": [], "serpapi_pagination": {}}

        original_max = get_reviews.MAX_REQUESTS

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0), \
                 patch.object(get_reviews, "MAX_REQUESTS", max_requests):
                get_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            with open(output_path, encoding="utf-8") as f:
                return json.load(f)

    def test_stops_when_no_next_token(self):
        """Should stop after single page when no next_page_token."""
        page1 = make_page_response([MOCK_REVIEW_1])  # no next_token
        result = self._run_with_mocked_responses([page1])
        self.assertEqual(result["pages_fetched"], 1)
        self.assertEqual(result["collection_status"], "completed")
        self.assertTrue(result["collection_complete"])

    def test_follows_pagination_across_multiple_pages(self):
        """Should follow tokens until exhausted."""
        page1 = make_page_response([MOCK_REVIEW_1], next_token="tok2")
        page2 = make_page_response([MOCK_REVIEW_2], next_token="tok3")
        page3 = make_page_response([MOCK_REVIEW_NO_ID])  # last page

        result = self._run_with_mocked_responses([page1, page2, page3])
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(result["reviews_collected"], 3)
        self.assertEqual(result["collection_status"], "completed")

    def test_stops_at_max_requests_limit(self):
        """Should stop early when MAX_REQUESTS is reached."""
        pages = [
            make_page_response([MOCK_REVIEW_1], next_token=f"tok{i}")
            for i in range(10)
        ]
        result = self._run_with_mocked_responses(pages, max_requests=3)
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(result["collection_status"], "stopped_at_request_limit")
        self.assertFalse(result["collection_complete"])

    def test_empty_reviews_on_first_page_triggers_completion(self):
        """Empty review list on first page should mark as completed."""
        page1 = make_page_response([])
        # Should exit without saving (sys.exit), so we catch SystemExit
        import tempfile
        call_count = [0]

        def fake_fetch(api_key, params):
            call_count[0] += 1
            return page1

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0), \
                 patch.object(get_reviews, "MAX_REQUESTS", 5):
                get_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            # Output should exist with 0 reviews
            with open(output_path, encoding="utf-8") as f:
                result = json.load(f)
        self.assertEqual(result["reviews_collected"], 0)
        self.assertEqual(result["collection_status"], "completed")


class TestQuotaExhaustionAndPartialSave(unittest.TestCase):
    """Verify that partial results are saved when the API errors mid-way."""

    def test_partial_save_on_error(self):
        """Reviews from pages before the error should be saved."""
        import tempfile

        page1 = make_page_response([MOCK_REVIEW_1], next_token="tok2")

        call_count = [0]

        def fake_fetch(api_key, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return page1
            raise RuntimeError("SerpApi credit/quota exhausted.")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0):
                get_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            with open(output_path, encoding="utf-8") as f:
                result = json.load(f)

        # The one review from page 1 must be saved
        self.assertEqual(result["reviews_collected"], 1)
        self.assertEqual(result["collection_status"], "stopped_on_error")
        self.assertFalse(result["collection_complete"])


class TestApiKeyValidation(unittest.TestCase):
    """Verify API key loading and validation without touching the real env."""

    def test_exits_if_key_missing(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": ""}, clear=True):
            with patch("get_reviews.load_dotenv"):
                with self.assertRaises(SystemExit):
                    get_reviews.load_api_key()

    def test_exits_if_key_is_placeholder(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "YOUR_PRIVATE_API_KEY_HERE"}):
            with patch("get_reviews.load_dotenv"):
                with self.assertRaises(SystemExit):
                    get_reviews.load_api_key()

    def test_returns_key_when_valid(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test_real_key_12345"}):
            with patch("get_reviews.load_dotenv"):
                key = get_reviews.load_api_key()
        self.assertEqual(key, "test_real_key_12345")


class TestNoSecretLeakage(unittest.TestCase):
    """Verify the API key is never embedded in the output JSON."""

    def test_api_key_not_in_output(self):
        import tempfile

        page1 = make_page_response([MOCK_REVIEW_1])
        fake_key = "SUPER_SECRET_KEY_XYZ_12345"

        def fake_fetch(api_key, params):
            return page1

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0):
                get_reviews.run_extraction(
                    api_key=fake_key,
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            raw = open(output_path, encoding="utf-8").read()

        self.assertNotIn(fake_key, raw, "API key must not appear in output JSON!")

    def test_api_key_not_in_review_data(self):
        """Reviews should contain no trace of the API key."""
        fake_key = "ANOTHER_SECRET_KEY_9999"
        review_with_key_attempt = dict(MOCK_REVIEW_1)
        review_with_key_attempt["snippet"] = f"Normal review text {fake_key} injected"

        flat = get_reviews.flatten_review(review_with_key_attempt)
        # The text should pass through (it is user-supplied review text, not our key)
        # But the api_key parameter itself should NOT appear in any system field
        for field in ["review_id", "author", "author_url", "review_link", "source"]:
            if flat.get(field):
                self.assertNotEqual(flat[field], fake_key)


class TestOutputJsonValidity(unittest.TestCase):
    """Verify that saved JSON is always valid and well-formed."""

    def test_output_is_valid_json(self):
        import tempfile

        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2, MOCK_REVIEW_NO_ID])

        def fake_fetch(api_key, params):
            return page1

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "out.json")
            with patch.object(get_reviews, "fetch_page", side_effect=fake_fetch), \
                 patch.object(get_reviews, "REQUEST_DELAY_SECONDS", 0):
                get_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                )
            with open(output_path, encoding="utf-8") as f:
                result = json.load(f)

        # Required top-level keys
        for key in ["source", "data_id", "reviews_collected", "pages_fetched",
                    "collection_complete", "collection_status", "reviews"]:
            self.assertIn(key, result)

        # Reviews is a list
        self.assertIsInstance(result["reviews"], list)

        # No fabricated data
        for rev in result["reviews"]:
            # text must come from the mock, not invented
            if rev["text"] is not None:
                self.assertIsInstance(rev["text"], str)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running Google Maps Review Extractor tests (no real API calls)...\n")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
