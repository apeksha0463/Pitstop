"""
scripts/tests/test_extract_reviews.py
=====================================
Tests for scripts/extract_reviews.py — the main extraction loop.

Covers:
  - Deduplication across pages
  - Pagination (single page, multi-page, max-request limit)
  - Partial-save on API error
  - API key not present in output JSON
  - Output schema validity
  - Review images: none / one / many, downloads, failures, duplicates
  - Pagination parameters, request budget, credit exhaustion, invalid keys
  - Progress saved after every page, raw responses redacted, re-run merging
  - Zero-credit rebuild from cached responses

All tests run without real SerpApi calls or network access (fetch_page
and the image session are mocked).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Ensure repo root and scripts dir are on sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import extract_reviews
    from scripts.utils import flatten_review, make_review_fingerprint
except ImportError:
    import extract_reviews
    from utils import flatten_review, make_review_fingerprint

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
            patch.object(extract_reviews, "fetch_page", side_effect=fake_fetch),
            patch.object(extract_reviews, "REQUEST_DELAY_SECONDS", 0),
            patch.object(extract_reviews, "save_json") as mock_save,
        ):
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
                patch.object(extract_reviews, "fetch_page", side_effect=fake_fetch),
                patch.object(extract_reviews, "save_json") as mock_save,
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
                patch.object(extract_reviews, "fetch_page", return_value=page1),
                patch.object(extract_reviews, "save_json") as mock_save,
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


# ---------------------------------------------------------------------------
# Review-image pipeline (pagination + images + persistence), fully mocked
# ---------------------------------------------------------------------------

import contextlib
import io
import tempfile

try:
    from scripts import utils as _utils
    from scripts import extract_review_images as _eri
except ImportError:
    import utils as _utils
    import extract_review_images as _eri

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x07" * 100
SECRET_KEY = "a1b2c3d4" * 8


def img_review(review_id, urls, text="Clean washroom."):
    return {
        "review_id": review_id,
        "user": {"name": f"User {review_id}", "link": "https://maps.test/u", "thumbnail": "https://avatar.test/x"},
        "rating": 4,
        "date": "2 months ago",
        "snippet": text,
        "images": urls,
        "link": f"https://maps.test/r/{review_id}",
    }


class _ImageSession:
    def __init__(self, ok_urls):
        self.ok_urls = set(ok_urls)
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)

        class R:
            status_code = 200 if url in self.ok_urls else 403
            headers = {"Content-Type": "image/jpeg" if url in self.ok_urls else "text/html"}

            def iter_content(self_inner, chunk_size=65536):
                yield JPEG + url.encode()

            def close(self_inner):
                pass

        return R()


class _PipelineBase(unittest.TestCase):
    """Redirects every data/ path into a temporary project root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        data = self.root / "data"
        self._patches = [
            patch.object(_utils, "PROJECT_ROOT", self.root),
            patch.object(_utils, "DATA_DIR", data),
            patch.object(_utils, "RAW_DATA_DIR", data / "raw_data"),
            patch.object(_utils, "EXTRACTED_PAGES_DIR", data / "extracted_pages"),
            patch.object(_utils, "EXTRACTED_IMAGES_DIR", data / "extracted_images"),
            patch.object(_utils, "OUTPUT_DIR", data / "output"),
            patch.object(_utils, "PROCESSED_DIR", data / "processed"),
            patch.object(_eri.time, "sleep"),
        ]
        for p in self._patches:
            p.start()
        self.calls = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def run_pages(self, pages, *, ok_urls=(), max_requests=10, downloader=True, output_path=None, quiet=True):
        """pages: list of dicts or Exceptions returned/raised by successive fetch_page calls."""
        seq = list(pages)

        def fake_fetch(api_key, params, **kw):
            self.calls.append(dict(params))
            item = seq.pop(0) if seq else {"reviews": [], "serpapi_pagination": {}}
            if isinstance(item, BaseException):
                raise item
            return item

        self.session = _ImageSession(ok_urls)
        dl = _eri.ReviewImageDownloader("0xAB:0xCD", session=self.session, delay=0) if downloader else None
        buf = io.StringIO()
        with patch.object(extract_reviews, "fetch_page", side_effect=fake_fetch), \
             (contextlib.redirect_stdout(buf) if quiet else contextlib.nullcontext()):
            result = extract_reviews.run_extraction(
                SECRET_KEY, "0xAB:0xCD", output_path, max_requests=max_requests, delay=0, downloader=dl,
            )
        self.stdout = buf.getvalue()
        return result

    def output_file(self):
        files = list((self.root / "data" / "output").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return files[0]

    def all_written_text(self):
        return "\n".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*.json"))


class TestReviewImagePipeline(_PipelineBase):

    def test_reviews_with_and_without_images(self):
        page = make_page_response([
            img_review("r1", []),
            img_review("r2", ["https://img.test/1"]),
            img_review("r3", ["https://img.test/2", "https://img.test/3"]),
        ])
        out = self.run_pages([page], ok_urls={"https://img.test/1", "https://img.test/2", "https://img.test/3"})
        self.assertEqual(out["reviews_collected"], 3)
        self.assertEqual(out["reviews_with_images"], 2)
        self.assertEqual(out["images_found"], 3)
        self.assertEqual(out["images_downloaded"], 3)
        by_id = {r["review_id"]: r for r in out["reviews"]}
        self.assertEqual(by_id["r1"]["images"], [])
        self.assertEqual(by_id["r2"]["images"][0]["local_path"], "data/extracted_images/0xAB_0xCD/r2_1.jpg")
        self.assertEqual(by_id["r3"]["images"][1]["download_status"], "downloaded")
        self.assertTrue((self.root / by_id["r3"]["images"][1]["local_path"]).exists())

    def test_reviewer_avatar_never_downloaded_as_review_image(self):
        out = self.run_pages([make_page_response([img_review("r1", [])])])
        self.assertEqual(out["images_found"], 0)
        self.assertEqual(self.session.calls, [])

    def test_failed_downloads_recorded_not_fatal(self):
        page = make_page_response([img_review("r1", ["https://img.test/ok", "https://img.test/forbidden"])])
        out = self.run_pages([page], ok_urls={"https://img.test/ok"})
        statuses = [i["download_status"] for i in out["reviews"][0]["images"]]
        self.assertEqual(statuses, ["downloaded", "failed"])
        self.assertEqual(out["images_failed"], 1)
        self.assertEqual(out["reviews"][0]["images"][1]["image_url"], "https://img.test/forbidden")
        self.assertEqual(out["collection_status"], "completed")

    def test_review_metadata_preserved(self):
        text = "Washroom was spotless 👍\nWould stop again."
        out = self.run_pages([make_page_response([img_review("r1", [], text=text)])])
        rev = out["reviews"][0]
        self.assertEqual(rev["text"], text)
        self.assertEqual(rev["rating"], 4)
        self.assertEqual(rev["date"], "2 months ago")
        self.assertEqual(rev["author"], "User r1")

    def test_duplicate_review_across_pages_keeps_images_once(self):
        p1 = make_page_response([img_review("r1", ["https://img.test/1"])], next_token="t2")
        p2 = make_page_response([img_review("r1", ["https://img.test/1", "https://img.test/2"])])
        out = self.run_pages([p1, p2], ok_urls={"https://img.test/1", "https://img.test/2"})
        self.assertEqual(out["reviews_collected"], 1)
        self.assertEqual(out["duplicates_skipped"], 1)
        self.assertEqual([i["image_url"] for i in out["reviews"][0]["images"]],
                         ["https://img.test/1", "https://img.test/2"])
        self.assertEqual(self.session.calls.count("https://img.test/1"), 1)

    def test_shared_image_between_reviews_downloaded_once(self):
        page = make_page_response([img_review("r1", ["https://img.test/same"]),
                                   img_review("r2", ["https://img.test/same"])])
        out = self.run_pages([page], ok_urls={"https://img.test/same"})
        self.assertEqual(self.session.calls, ["https://img.test/same"])
        self.assertEqual(out["images_found"], 2)
        self.assertEqual(out["unique_image_files"], 1)
        paths = {r["review_id"]: r["images"][0]["local_path"] for r in out["reviews"]}
        self.assertEqual(paths["r1"], paths["r2"])


class TestPaginationParams(_PipelineBase):

    def test_first_request_params_and_token_follow_up(self):
        p1 = make_page_response([img_review("r1", [])], next_token="TOKEN_2")
        p2 = make_page_response([img_review("r2", [])])
        self.run_pages([p1, p2])
        first, second = self.calls
        self.assertEqual(first, {"engine": "google_maps_reviews", "data_id": "0xAB:0xCD",
                                 "hl": "en", "sort_by": "newestFirst"})
        self.assertNotIn("num", first, "num is only sent with next_page_token")
        self.assertEqual(second["next_page_token"], "TOKEN_2")
        self.assertEqual(second["num"], 20)
        self.assertNotIn("api_key", first)

    def test_missing_token_marks_complete(self):
        out = self.run_pages([make_page_response([img_review("r1", [])])])
        self.assertTrue(out["collection_complete"])
        self.assertEqual(len(self.calls), 1)

    def test_empty_page_with_token_is_not_complete(self):
        out = self.run_pages([make_page_response([], next_token="t2")])
        self.assertEqual(out["collection_status"], "stopped_empty_page")
        self.assertFalse(out["collection_complete"])

    def test_request_budget_exhausted(self):
        pages = [make_page_response([img_review(f"r{i}", [])], next_token=f"t{i}") for i in range(5)]
        out = self.run_pages(pages, max_requests=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(out["api_requests_used"], 2)
        self.assertEqual(out["collection_status"], "stopped_at_request_limit")
        self.assertFalse(out["collection_complete"])
        self.assertIn("--max-requests", out["stop_reason"])


class TestApiErrorsAndPartialSaves(_PipelineBase):

    def test_credits_exhausted_mid_run_keeps_partial(self):
        p1 = make_page_response([img_review("r1", ["https://img.test/1"])], next_token="t2")
        out = self.run_pages([p1, _utils.CreditsExhaustedError("SerpApi credits exhausted.")],
                             ok_urls={"https://img.test/1"})
        self.assertEqual(out["collection_status"], "stopped_credits_exhausted")
        self.assertFalse(out["collection_complete"])
        saved = json.loads(self.output_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["reviews_collected"], 1)
        self.assertEqual(saved["images_downloaded"], 1)

    def test_invalid_key_on_first_page_saves_nothing(self):
        out = self.run_pages([_utils.InvalidApiKeyError("Invalid API key.")])
        self.assertIsNone(out)
        self.assertFalse((self.root / "data" / "output").exists()
                         and any((self.root / "data" / "output").iterdir()))

    def test_progress_saved_after_every_page(self):
        p1 = make_page_response([img_review("r1", [])], next_token="t2")
        p2 = make_page_response([img_review("r2", [])], next_token="t3")
        snapshots = []
        real_save = _utils.save_json

        def spy(data, path):
            real_save(data, path)
            if Path(path).parent.name == "output":
                snapshots.append((data["collection_status"], data["reviews_collected"]))

        with patch.object(extract_reviews, "save_json", side_effect=spy):
            self.run_pages([p1, p2, KeyboardInterrupt()])
        self.assertEqual(snapshots[:2], [("in_progress", 1), ("in_progress", 2)])
        self.assertEqual(snapshots[-1], ("interrupted_by_user", 2))

    def test_raw_and_page_files_written_and_redacted(self):
        p1 = make_page_response([img_review("r1", ["https://img.test/1"])])
        p1["search_parameters"] = {"engine": "google_maps_reviews", "api_key": SECRET_KEY}
        p1["search_metadata"]["json_endpoint"] = f"https://serpapi.com/searches/x.json?api_key={SECRET_KEY}"
        self.run_pages([p1], ok_urls={"https://img.test/1"})
        raw = self.root / "data" / "raw_data" / "serpapi" / "0xAB_0xCD_page001.json"
        page = self.root / "data" / "extracted_pages" / "0xAB_0xCD_page001.json"
        self.assertTrue(raw.exists())
        self.assertTrue(page.exists())
        self.assertEqual(json.loads(page.read_text(encoding="utf-8"))["reviews"][0]["review_id"], "r1")
        self.assertNotIn(SECRET_KEY, self.all_written_text())
        self.assertNotIn(SECRET_KEY, self.stdout)

    def test_rerun_merges_with_previous_output(self):
        self.run_pages([make_page_response([img_review("old", [])])])
        out = self.run_pages([make_page_response([img_review("new", [])])])
        self.assertEqual({r["review_id"] for r in out["reviews"]}, {"old", "new"})
        self.assertEqual(out["previous_reviews_retained"], 1)
        self.assertEqual(len(list((self.root / "data" / "output").glob("*.json"))), 1)

    def test_output_filename_uses_restaurant_name(self):
        self.run_pages([make_page_response([img_review("r1", [])])])
        self.assertEqual(self.output_file().name, "Test_Restaurant_0xAB_0xCD.json")


class TestCacheAndCli(_PipelineBase):

    def test_from_cache_uses_zero_api_requests(self):
        self.run_pages([make_page_response([img_review("r1", ["https://img.test/1"])])],
                       ok_urls={"https://img.test/1"})
        with patch.object(extract_reviews, "fetch_page") as mock_fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            out = extract_reviews.run_cli("0xAB:0xCD", from_cache=True,
                                          downloader=_eri.ReviewImageDownloader("0xAB:0xCD", session=_ImageSession(()), delay=0))
        mock_fetch.assert_not_called()
        self.assertTrue(out["from_cache"])
        self.assertEqual(out["api_requests_used"], 0)
        self.assertEqual(out["reviews"][0]["images"][0]["download_status"], "already_downloaded")

    def test_from_cache_without_cache_fails_cleanly(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(extract_reviews.run_cli("0x1:0x2", from_cache=True))

    def test_cli_invalid_key_stops_before_any_search(self):
        with patch.object(extract_reviews, "load_api_key", return_value=SECRET_KEY), \
             patch.object(extract_reviews, "validate_api_key",
                          side_effect=_utils.InvalidApiKeyError("SerpApi rejected the API key.")), \
             patch.object(extract_reviews, "fetch_page") as mock_fetch, \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertIsNone(extract_reviews.run_cli("0xAB:0xCD"))
        mock_fetch.assert_not_called()
        self.assertNotIn(SECRET_KEY, buf.getvalue())

    def test_cli_budget_capped_by_remaining_credits(self):
        pages = [make_page_response([img_review(f"r{i}", [])], next_token=f"t{i}") for i in range(5)]
        with patch.object(extract_reviews, "load_api_key", return_value=SECRET_KEY), \
             patch.object(extract_reviews, "validate_api_key",
                          return_value={"plan_name": "Free Plan", "total_searches_left": 2}), \
             patch.object(extract_reviews, "fetch_page", side_effect=pages) as mock_fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            out = extract_reviews.run_cli("0xAB:0xCD", max_requests=5, downloader=None)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(out["max_requests"], 2)

    def test_main_prompts_for_data_id(self):
        try:
            from scripts import extract_review_images as entry
        except ImportError:
            import extract_review_images as entry
        with patch("builtins.input", return_value="  0xAB:0xCD  "), \
             patch.object(extract_reviews, "run_cli", return_value={"ok": True}) as mock_cli, \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            code = entry.main([])
        self.assertEqual(code, 0)
        self.assertEqual(mock_cli.call_args.args[0], "0xAB:0xCD")
        self.assertIn("PITSTOP REVIEW IMAGE EXTRACTOR", buf.getvalue())

    def test_main_rejects_bad_data_id(self):
        try:
            from scripts import extract_review_images as entry
        except ImportError:
            import extract_review_images as entry
        with patch("builtins.input", return_value="not-an-id"), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                entry.main([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
