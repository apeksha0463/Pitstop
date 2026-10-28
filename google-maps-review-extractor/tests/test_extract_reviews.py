"""
tests/test_extract_reviews.py
==============================
Tests for scripts/extract_reviews.py — the main extraction loop.

Covers:
  - Deduplication across pages
  - Pagination (single page, multi-page, max-request limit)
  - Partial-save on API error
  - API key not present in output JSON
  - Output schema validity

All tests run without real SerpApi calls (fetch_page is mocked).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Ensure project root is importable when run from tests/ or project root
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent      # tests/
_ROOT = _HERE.parent                          # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import extract_reviews
from scripts.utils import flatten_review, make_review_fingerprint

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MOCK_REVIEW_1 = {
    "review_id": "rev_001",
    "user": {
        "name": "Alice Smith",
        "link": "https://maps.google.com/user/alice",
        "local_guide": True,
        "reviews": 42,
    },
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
    "user": {
        "name": "Bob Jones",
        "link": "https://maps.google.com/user/bob",
        "local_guide": False,
        "reviews": 5,
    },
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
    resp["serpapi_pagination"] = (
        {"next_page_token": next_token} if next_token else {}
    )
    return resp


# ---------------------------------------------------------------------------
# Helper: run extraction against mocked responses
# ---------------------------------------------------------------------------

def _run_mocked(responses, max_requests=20, save_raw=False):
    """
    Patch fetch_page to return *responses* in sequence, run run_extraction,
    and return the parsed output dict.
    """
    import tempfile

    call_count = [0]

    def fake_fetch(api_key, params, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(responses):
            return responses[idx]
        return {"reviews": [], "serpapi_pagination": {}}

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "out.json"
        with (
            patch("scripts.extract_reviews.fetch_page", side_effect=fake_fetch),
            patch("scripts.extract_reviews.REQUEST_DELAY_SECONDS", 0),
            patch("scripts.extract_reviews.save_json") as mock_save,
        ):
            # Route save_json calls to real filesystem in tmpdir
            import json as _json

            def real_save(data, path, **kw):
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    _json.dump(data, fh, ensure_ascii=False, indent=2)

            mock_save.side_effect = real_save

            extract_reviews.run_extraction(
                api_key="FAKE_KEY",
                data_id="0xFAKE",
                output_path=output_path,
                max_requests=max_requests,
                delay=0,
                save_every=0,
                save_raw_pages=save_raw,
            )

        with open(output_path, encoding="utf-8") as fh:
            return _json.load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeduplication(unittest.TestCase):

    def test_duplicate_review_skipped_across_pages(self):
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2], next_token="tok2")
        page2 = make_page_response([MOCK_REVIEW_1])          # duplicate

        result = _run_mocked([page1, page2])
        self.assertEqual(result["reviews_collected"], 2)
        self.assertEqual(result["duplicates_skipped"], 1)

    def test_no_false_positives_for_distinct_reviews(self):
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2])

        result = _run_mocked([page1])
        self.assertEqual(result["reviews_collected"], 2)
        self.assertEqual(result["duplicates_skipped"], 0)


class TestPagination(unittest.TestCase):

    def test_stops_when_no_next_token(self):
        page1 = make_page_response([MOCK_REVIEW_1])

        result = _run_mocked([page1])
        self.assertEqual(result["pages_fetched"], 1)
        self.assertEqual(result["collection_status"], "completed")
        self.assertTrue(result["collection_complete"])

    def test_follows_multiple_pages(self):
        page1 = make_page_response([MOCK_REVIEW_1], next_token="tok2")
        page2 = make_page_response([MOCK_REVIEW_2], next_token="tok3")
        page3 = make_page_response([MOCK_REVIEW_NO_ID])

        result = _run_mocked([page1, page2, page3])
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(result["reviews_collected"], 3)
        self.assertEqual(result["collection_status"], "completed")

    def test_stops_at_max_requests(self):
        pages = [
            make_page_response([MOCK_REVIEW_1], next_token=f"tok{i}")
            for i in range(10)
        ]
        result = _run_mocked(pages, max_requests=3)
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(result["collection_status"], "stopped_at_request_limit")
        self.assertFalse(result["collection_complete"])

    def test_empty_first_page_marks_completed(self):
        page1 = make_page_response([])

        result = _run_mocked([page1])
        self.assertEqual(result["reviews_collected"], 0)
        self.assertEqual(result["collection_status"], "completed")


class TestPartialSaveOnError(unittest.TestCase):

    def test_partial_results_saved_after_api_error(self):
        import tempfile
        import json as _json

        page1 = make_page_response([MOCK_REVIEW_1], next_token="tok2")
        call_count = [0]

        def fake_fetch(api_key, params, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return page1
            raise RuntimeError("SerpApi credit/quota exhausted.")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.json"

            with (
                patch("scripts.extract_reviews.fetch_page", side_effect=fake_fetch),
                patch("scripts.extract_reviews.save_json") as mock_save,
            ):
                def real_save(data, path, **kw):
                    p = Path(path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with open(p, "w", encoding="utf-8") as fh:
                        _json.dump(data, fh, ensure_ascii=False, indent=2)

                mock_save.side_effect = real_save

                extract_reviews.run_extraction(
                    api_key="FAKE_KEY",
                    data_id="0xFAKE",
                    output_path=output_path,
                    max_requests=10,
                    delay=0,
                    save_raw_pages=False,
                )

            with open(output_path, encoding="utf-8") as fh:
                result = _json.load(fh)

        self.assertEqual(result["reviews_collected"], 1)
        self.assertEqual(result["collection_status"], "stopped_on_error")
        self.assertFalse(result["collection_complete"])


class TestNoSecretLeakage(unittest.TestCase):

    def test_api_key_not_in_output_json(self):
        fake_key = "SUPER_SECRET_KEY_XYZ_12345"
        page1 = make_page_response([MOCK_REVIEW_1])

        import tempfile
        import json as _json

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.json"

            with (
                patch("scripts.extract_reviews.fetch_page", return_value=page1),
                patch("scripts.extract_reviews.save_json") as mock_save,
            ):
                def real_save(data, path, **kw):
                    p = Path(path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with open(p, "w", encoding="utf-8") as fh:
                        _json.dump(data, fh, ensure_ascii=False, indent=2)

                mock_save.side_effect = real_save

                extract_reviews.run_extraction(
                    api_key=fake_key,
                    data_id="0xFAKE",
                    output_path=output_path,
                    max_requests=5,
                    delay=0,
                    save_raw_pages=False,
                )

            raw = open(output_path, encoding="utf-8").read()

        self.assertNotIn(fake_key, raw, "API key must never appear in output JSON!")


class TestOutputSchema(unittest.TestCase):

    def test_required_keys_present(self):
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_2, MOCK_REVIEW_NO_ID])
        result = _run_mocked([page1])

        required = [
            "source", "data_id", "reviews_collected", "pages_fetched",
            "collection_complete", "collection_status", "reviews",
        ]
        for key in required:
            self.assertIn(key, result, f"Missing key: {key}")

    def test_reviews_is_a_list(self):
        page1 = make_page_response([MOCK_REVIEW_1])
        result = _run_mocked([page1])
        self.assertIsInstance(result["reviews"], list)

    def test_review_text_is_string_or_none(self):
        page1 = make_page_response([MOCK_REVIEW_1, MOCK_REVIEW_NO_ID])
        result = _run_mocked([page1])
        for rev in result["reviews"]:
            if rev["text"] is not None:
                self.assertIsInstance(rev["text"], str)


if __name__ == "__main__":
    print("Running extract_reviews tests (no real API calls)...\n")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
