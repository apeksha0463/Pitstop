"""
scripts/tests/test_paths.py
===========================
Tests for path utilities and configuration in scripts/utils.py.

Covers:
  - PROJECT_ROOT resolves to the repository root
  - SCRIPTS_DIR, DATA_DIR, and SOURCES_DIR are children of PROJECT_ROOT
  - OUTPUT_DIR is inside DATA_DIR (data/output/)
  - All data subdirectories are children of DATA_DIR
  - make_output_path returns a Path inside OUTPUT_DIR
  - make_output_path embeds the safe data_id and a timestamp
  - make_output_path is <data_id>_reviews.json; legacy output files are found
  - make_raw_page_path returns a Path inside EXTRACTED_PAGES_DIR
  - save_json creates intermediate directories automatically
  - ensure_dirs creates all required directories
  - load_api_key validates keys correctly
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
except ImportError:
    import utils


class TestProjectRoot(unittest.TestCase):

    def test_project_root_is_not_scripts_dir(self):
        self.assertNotEqual(utils.PROJECT_ROOT.name, "scripts")

    def test_scripts_contains_requirements_txt(self):
        self.assertTrue((utils.SCRIPTS_DIR / "requirements.txt").exists())

    def test_data_dir_is_child_of_project_root(self):
        self.assertEqual(utils.DATA_DIR.parent, utils.PROJECT_ROOT)

    def test_sources_dir_is_child_of_project_root(self):
        self.assertEqual(utils.SOURCES_DIR.parent, utils.PROJECT_ROOT)

    def test_output_dir_is_child_of_data_dir(self):
        self.assertEqual(utils.OUTPUT_DIR.parent, utils.DATA_DIR)


class TestDataSubdirConstants(unittest.TestCase):

    def test_raw_data_dir_is_under_data(self):
        self.assertEqual(utils.RAW_DATA_DIR.parent, utils.DATA_DIR)

    def test_extracted_pages_dir_is_under_data(self):
        self.assertEqual(utils.EXTRACTED_PAGES_DIR.parent, utils.DATA_DIR)

    def test_extracted_images_dir_is_under_data(self):
        self.assertEqual(utils.EXTRACTED_IMAGES_DIR.parent, utils.DATA_DIR)

    def test_processed_dir_is_under_data(self):
        self.assertEqual(utils.PROCESSED_DIR.parent, utils.DATA_DIR)


class TestMakeOutputPath(unittest.TestCase):

    def _run(self, data_id, restaurant_name=""):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_output = Path(tmpdir)
            with patch.object(utils, "OUTPUT_DIR", fake_output):
                return utils.make_output_path(data_id, restaurant_name)

    def test_returns_path_object(self):
        path = self._run("0xABCD:0x1234")
        self.assertIsInstance(path, Path)

    def test_path_has_json_extension(self):
        path = self._run("0xABCD:0x1234")
        self.assertEqual(path.suffix, ".json")

    def test_safe_data_id_embedded_in_name(self):
        path = self._run("0xABCD:0x1234")
        self.assertIn("0xABCD_0x1234", path.name)

    def test_colons_replaced_in_filename(self):
        path = self._run("0xABCD:0x1234")
        self.assertNotIn(":", path.name)

    def test_filename_is_restaurant_id_reviews_json(self):
        self.assertEqual(self._run("0xABCD:0x1234", "Swathi Delicacy").name, "0xABCD_0x1234_reviews.json")
        self.assertEqual(self._run("0xABCD:0x1234").name, "0xABCD_0x1234_reviews.json")

    def test_legacy_output_files_found_newest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            with patch.object(utils, "OUTPUT_DIR", out):
                old = out / "restaurant_reviews_0xABCD_0x1234_20261027_115048.json"
                named = out / "Cafe_0xABCD_0x1234.json"
                for i, p in enumerate((old, named, out / "0xABCD_0x1234_reviews.json", out / "Cafe_0x1_0x2.json")):
                    p.write_text("{}", encoding="utf-8")
                    os.utime(p, (1000 + i, 1000 + i))
                self.assertEqual(utils.legacy_output_paths("0xABCD:0x1234"), [named, old])

    def test_one_stable_file_per_restaurant(self):
        """Re-runs for the same restaurant target the same file (merged, not duplicated)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "OUTPUT_DIR", Path(tmpdir)):
                p1 = utils.make_output_path("0xABCD:0x1234", "Cafe")
                p2 = utils.make_output_path("0xABCD:0x1234", "Cafe")
                p3 = utils.make_output_path("0xABCD:0x9999", "Cafe")
        self.assertEqual(p1, p2)
        self.assertNotEqual(p1, p3)

    def test_unsafe_restaurant_name_sanitised(self):
        path = self._run("0xABCD:0x1234", '../..\\evil:<name>?')
        self.assertNotIn("..", path.name)
        for ch in '\\/:<>?':
            self.assertNotIn(ch, path.name)


class TestMakeRawPagePath(unittest.TestCase):

    def test_returns_path_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "RAW_DATA_DIR", Path(tmpdir)):
                path = utils.make_raw_page_path("0xABCD:0x1234", 1)
        self.assertIsInstance(path, Path)

    def test_page_number_zero_padded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "RAW_DATA_DIR", Path(tmpdir)):
                path = utils.make_raw_page_path("0xABCD:0x1234", 5)
        self.assertIn("page005", path.name)

    def test_raw_page_path_in_raw_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_raw = Path(tmpdir) / "raw_data"
            with patch.object(utils, "RAW_DATA_DIR", fake_raw):
                path = utils.make_raw_page_path("0xABCD:0x1234", 1)
        self.assertEqual(path.parent, fake_raw / "serpapi")

    def test_extracted_page_path_in_extracted_pages_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_pages = Path(tmpdir) / "extracted_pages"
            with patch.object(utils, "EXTRACTED_PAGES_DIR", fake_pages):
                path = utils.make_extracted_page_path("0xABCD:0x1234", 2)
        self.assertEqual(path.parent, fake_pages)
        self.assertIn("page002", path.name)


class TestSaveJson(unittest.TestCase):

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deep = Path(tmpdir) / "a" / "b" / "c" / "out.json"
            utils.save_json({"x": 1}, deep)
            self.assertTrue(deep.exists())

    def test_content_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "test.json"
            utils.save_json({"key": "value", "num": 42}, p)
            loaded = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(loaded["key"], "value")
        self.assertEqual(loaded["num"], 42)

    def test_utf8_unicode_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "uni.json"
            utils.save_json({"text": "中文内容"}, p)
            raw = p.read_text(encoding="utf-8")
        self.assertIn("中文内容", raw)

    def test_accepts_path_or_str(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p_str = str(Path(tmpdir) / "str_path.json")
            utils.save_json({"ok": True}, p_str)
            self.assertTrue(Path(p_str).exists())


class TestMakeReviewFingerprint(unittest.TestCase):

    def test_uses_review_id_when_present(self):
        review = {"review_id": "rev_001", "user": {}, "date": "", "snippet": ""}
        fp = utils.make_review_fingerprint(review)
        self.assertEqual(fp, "rev_001")

    def test_fallback_uses_author_date_text(self):
        review = {
            "user": {"name": "Alice"},
            "date": "2 weeks ago",
            "snippet": "Good food",
        }
        fp = utils.make_review_fingerprint(review)
        self.assertIn("Alice", fp)
        self.assertIn("2 weeks ago", fp)

    def test_fallback_is_deterministic(self):
        review = {"user": {"name": "Bob"}, "date": "1 month ago", "snippet": "OK"}
        fp1 = utils.make_review_fingerprint(review)
        fp2 = utils.make_review_fingerprint(review)
        self.assertEqual(fp1, fp2)


class TestFlattenReview(unittest.TestCase):

    MOCK_REVIEW = {
        "review_id": "r1",
        "user": {"name": "Alice", "link": "https://g.co/alice", "local_guide": True, "reviews": 10},
        "rating": 5,
        "date": "1 week ago",
        "iso_date": "2026-10-01T00:00:00Z",
        "snippet": "Great!",
        "language": "en",
        "translated": False,
        "likes": 3,
        "owner_answer": None,
        "photos": [{"link": "https://photo.example.com/1.jpg"}],
        "link": "https://g.co/review/r1",
    }

    def test_all_expected_keys_present(self):
        flat = utils.flatten_review(self.MOCK_REVIEW)
        expected_keys = [
            "review_id", "author", "author_url", "author_local_guide",
            "author_reviews_count", "rating", "date", "iso_date",
            "iso_date_of_last_edit", "text", "language", "translated",
            "likes", "owner_response", "images_count", "images",
            "review_link", "source",
        ]
        for key in expected_keys:
            self.assertIn(key, flat, f"Missing key: {key}")

    def test_source_field(self):
        flat = utils.flatten_review(self.MOCK_REVIEW)
        self.assertEqual(flat["source"], "Google Maps via SerpApi")

    def test_api_key_not_injected(self):
        flat = utils.flatten_review(self.MOCK_REVIEW)
        raw = json.dumps(flat)
        self.assertNotIn("api_key", raw.lower())

    def test_review_text_preserved_verbatim(self):
        text = "  Clean washroom 🚻\nGreat dosa — ಚೆನ್ನಾಗಿದೆ!  "
        flat = utils.flatten_review({**self.MOCK_REVIEW, "snippet": text})
        self.assertEqual(flat["text"], text)

    def test_reviewer_avatar_is_not_a_review_image(self):
        raw = {**self.MOCK_REVIEW, "photos": None,
               "user": {"name": "A", "thumbnail": "https://avatar.test/a.jpg"}}
        flat = utils.flatten_review(raw)
        self.assertEqual(flat["images"], [])


class TestExtractPlaceInfo(unittest.TestCase):

    def test_extracts_all_fields(self):
        data = {
            "place_info": {
                "title": "My Restaurant",
                "address": "1 Main St",
                "rating": 4.5,
                "reviews": 500,
                "type": "Cafe",
            }
        }
        info = utils.extract_place_info(data)
        self.assertEqual(info["restaurant_name"], "My Restaurant")
        self.assertEqual(info["address"], "1 Main St")
        self.assertEqual(info["rating"], 4.5)
        self.assertEqual(info["total_reviews_reported"], 500)

    def test_handles_empty_response(self):
        info = utils.extract_place_info({})
        self.assertEqual(info["restaurant_name"], "Unknown")
        self.assertIsNone(info["address"])
        self.assertIsNone(info["rating"])


class TestLoadApiKey(unittest.TestCase):

    def test_loads_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text("SERPAPI_API_KEY=test_valid_key_12345\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                key = utils.load_api_key(fake_env)
                self.assertEqual(key, "test_valid_key_12345")

    def test_exits_on_placeholder_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text("SERPAPI_API_KEY=YOUR_PRIVATE_API_KEY_HERE\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    utils.load_api_key(fake_env)


class TestLoadApiKeyExtra(unittest.TestCase):

    def test_loads_key_from_bom_encoded_env_file(self):
        """Notepad on Windows may save .env with a UTF-8 BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_bytes("﻿SERPAPI_API_KEY=bom_key_abcdefghijklmnop\n".encode("utf-8"))
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(utils.load_api_key(fake_env), "bom_key_abcdefghijklmnop")

    def test_env_file_overrides_stale_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text("SERPAPI_API_KEY=new_replacement_key_123456\n", encoding="utf-8")
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "old_revoked_key_99999999"}, clear=True):
                self.assertEqual(utils.load_api_key(fake_env), "new_replacement_key_123456")

    def test_exits_on_new_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text("SERPAPI_API_KEY=PASTE_YOUR_NEW_KEY_HERE\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    utils.load_api_key(fake_env)

    def test_exits_on_missing_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit):
                    utils.load_api_key(Path(tmpdir) / "does_not_exist.env")

    def test_error_messages_never_print_the_key(self):
        import contextlib
        import io
        secret = "short key"  # malformed: contains a space
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_env = Path(tmpdir) / ".env"
            fake_env.write_text(f"SERPAPI_API_KEY={secret}\n", encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    utils.load_api_key(fake_env)
        self.assertNotIn(secret, buf.getvalue())


def _fake_response(status=200, body=None):
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.status_code = status
    if body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = body
    return resp


SECRET = "k" * 64


class TestValidateApiKey(unittest.TestCase):

    def test_valid_key_returns_account_info_without_key(self):
        body = {"plan_name": "Free Plan", "total_searches_left": 42, "api_key": SECRET}
        with patch.object(utils.requests, "get", return_value=_fake_response(200, body)):
            info = utils.validate_api_key(SECRET)
        self.assertEqual(info["total_searches_left"], 42)
        self.assertNotIn(SECRET, json.dumps(info))

    def test_invalid_key_raises(self):
        body = {"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"}
        with patch.object(utils.requests, "get", return_value=_fake_response(401, body)):
            with self.assertRaises(utils.InvalidApiKeyError) as ctx:
                utils.validate_api_key(SECRET)
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_zero_searches_left_raises(self):
        body = {"plan_name": "Free Plan", "total_searches_left": 0}
        with patch.object(utils.requests, "get", return_value=_fake_response(200, body)):
            with self.assertRaises(utils.CreditsExhaustedError):
                utils.validate_api_key(SECRET)

    def test_network_failure_returns_none(self):
        err = utils.requests.exceptions.ConnectionError(f"url?api_key={SECRET}")
        with patch.object(utils.requests, "get", side_effect=err):
            self.assertIsNone(utils.validate_api_key(SECRET))


class TestFetchPageErrors(unittest.TestCase):

    def _fetch(self, response=None, side_effect=None):
        with patch.object(utils.requests, "get", return_value=response, side_effect=side_effect), \
             patch.object(utils.time, "sleep"):
            return utils.fetch_page(SECRET, {"engine": "google_maps_reviews", "data_id": "0x1:0x2"}, max_retries=2)

    def test_success_returns_json(self):
        data = self._fetch(_fake_response(200, {"reviews": [{"review_id": "a"}]}))
        self.assertEqual(data["reviews"][0]["review_id"], "a")

    def test_http_401_is_invalid_key(self):
        with self.assertRaises(utils.InvalidApiKeyError):
            self._fetch(_fake_response(401, {"error": "Invalid API key."}))

    def test_out_of_searches_is_credits_exhausted(self):
        with self.assertRaises(utils.CreditsExhaustedError):
            self._fetch(_fake_response(429, {"error": "Your account has run out of searches."}))

    def test_error_in_200_body_is_credits_exhausted(self):
        with self.assertRaises(utils.CreditsExhaustedError):
            self._fetch(_fake_response(200, {"error": "You have reached your monthly plan quota."}))

    def test_no_results_body_is_empty_page(self):
        data = self._fetch(_fake_response(200, {"error": "Google hasn't returned any results for this query."}))
        self.assertEqual(data["reviews"], [])

    def test_server_error_retried_then_raises(self):
        with patch.object(utils.requests, "get", return_value=_fake_response(503, None)) as mock_get, \
             patch.object(utils.time, "sleep"):
            with self.assertRaises(utils.SerpApiError):
                utils.fetch_page(SECRET, {"data_id": "0x1:0x2"}, max_retries=2)
        self.assertEqual(mock_get.call_count, 2)

    def test_unexpected_http_status_does_not_leak_url(self):
        with self.assertRaises(utils.SerpApiError) as ctx:
            self._fetch(_fake_response(403, None))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_connection_error_message_does_not_leak_key(self):
        err = utils.requests.exceptions.ConnectionError(f"https://serpapi.com/search.json?api_key={SECRET}")
        with self.assertRaises(utils.SerpApiError) as ctx:
            self._fetch(side_effect=err)
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_api_key_sent_only_as_request_param(self):
        with patch.object(utils.requests, "get", return_value=_fake_response(200, {"reviews": []})) as mock_get:
            params = {"engine": "google_maps_reviews", "data_id": "0x1:0x2"}
            utils.fetch_page(SECRET, params)
        self.assertEqual(mock_get.call_args.kwargs["params"]["api_key"], SECRET)
        self.assertNotIn("api_key", params, "caller's params dict must not be mutated")


class TestRedactSecrets(unittest.TestCase):

    def test_removes_api_key_fields_and_masks_values(self):
        payload = {
            "search_parameters": {"engine": "google_maps_reviews", "api_key": SECRET},
            "search_metadata": {"json_endpoint": f"https://serpapi.com/x.json?api_key={SECRET}&a=1"},
            "nested": [{"note": f"key is {SECRET}"}],
        }
        clean = utils.redact_secrets(payload, SECRET)
        text = json.dumps(clean)
        self.assertNotIn(SECRET, text)
        self.assertNotIn('"api_key"', text)
        self.assertIn("google_maps_reviews", text)
        self.assertIn(SECRET, json.dumps(payload), "original must be left untouched")

    def test_masks_api_key_query_param_even_without_secret(self):
        clean = utils.redact_secrets({"u": "https://x.test/?api_key=abc123&b=2"})
        self.assertNotIn("abc123", clean["u"])


class TestExtractReviewImages(unittest.TestCase):

    def test_no_images_field(self):
        self.assertEqual(utils.extract_review_images({"review_id": "r"}), [])

    def test_images_none_or_empty(self):
        self.assertEqual(utils.extract_review_images({"images": None}), [])
        self.assertEqual(utils.extract_review_images({"images": []}), [])

    def test_list_of_url_strings(self):
        imgs = utils.extract_review_images({"images": ["https://a.test/1", "https://a.test/2"]})
        self.assertEqual([i["image_url"] for i in imgs], ["https://a.test/1", "https://a.test/2"])
        self.assertEqual([i["image_index"] for i in imgs], [1, 2])

    def test_single_string(self):
        imgs = utils.extract_review_images({"images": "https://a.test/only"})
        self.assertEqual(len(imgs), 1)

    def test_objects_with_different_keys_and_metadata(self):
        raw = {"images": [
            {"image": "https://a.test/1", "width": 800, "height": 600},
            {"thumbnail": "https://a.test/2-thumb"},
            {"link": "https://a.test/3", "title": "Washroom"},
            {"original": "https://a.test/4", "thumbnail": "https://a.test/4-thumb"},
        ]}
        imgs = utils.extract_review_images(raw)
        self.assertEqual([i["image_url"] for i in imgs],
                         ["https://a.test/1", "https://a.test/2-thumb", "https://a.test/3", "https://a.test/4"])
        self.assertEqual(imgs[0]["width"], 800)
        self.assertEqual(imgs[2]["title"], "Washroom")
        self.assertEqual(imgs[3]["thumbnail"], "https://a.test/4-thumb")

    def test_legacy_photos_field(self):
        imgs = utils.extract_review_images({"photos": [{"link": "https://a.test/p"}]})
        self.assertEqual(imgs[0]["image_url"], "https://a.test/p")

    def test_junk_entries_dropped_not_invented(self):
        raw = {"images": [None, 42, "", "not a url", {"caption": "no url"}, "ftp://x/y", "https://ok.test/1"]}
        imgs = utils.extract_review_images(raw)
        self.assertEqual([i["image_url"] for i in imgs], ["https://ok.test/1"])

    def test_duplicate_urls_within_review_deduplicated(self):
        raw = {"images": ["https://a.test/1", "https://a.test/1"], "photos": [{"link": "https://a.test/1"}]}
        self.assertEqual(len(utils.extract_review_images(raw)), 1)


class TestCachedPages(unittest.TestCase):

    def test_iter_cached_pages_follows_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data" / "raw_data"
            with patch.object(utils, "RAW_DATA_DIR", raw), patch.object(utils, "PROJECT_ROOT", root):
                p1 = utils.make_raw_page_path("0x1:0x2", 1)
                utils.save_json({"page": 1}, p1)
                utils.save_json({"page": 99}, utils.make_raw_page_path("0x1:0x2", 2))  # stale, not in manifest
                utils.save_json({"pages": [p1.relative_to(root).as_posix()]},
                                utils.make_cache_manifest_path("0x1:0x2"))
                pages = list(utils.iter_cached_pages("0x1:0x2"))
        self.assertEqual(pages, [{"page": 1}])

    def test_no_manifest_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "RAW_DATA_DIR", Path(tmpdir)):
                self.assertEqual(list(utils.iter_cached_pages("0x1:0x2")), [])

    def test_save_json_is_atomic_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "x.json"
            utils.save_json({"a": 1}, p)
            utils.save_json({"a": 2}, p)
            self.assertEqual(utils.load_json(p), {"a": 2})
            self.assertEqual([f.name for f in Path(tmpdir).iterdir()], ["x.json"])


class TestInputValidation(unittest.TestCase):

    def test_valid_data_id(self):
        self.assertTrue(utils.is_valid_data_id("0x3bae19cd83892495:0xc4ee26c182749dfe"))

    def test_invalid_data_ids(self):
        for bad in ("", "abc", "0x123", "ChIJxyz", "0x1:0x2; rm -rf /"):
            self.assertFalse(utils.is_valid_data_id(bad), bad)

    def test_to_project_relative_uses_forward_slashes(self):
        rel = utils.to_project_relative(utils.EXTRACTED_IMAGES_DIR / "a" / "b.jpg")
        self.assertEqual(rel, "data/extracted_images/a/b.jpg")


class TestRepositoryLayout(unittest.TestCase):
    """Guards the required layout and secret protection (needs git)."""

    def _git(self, *args):
        import shutil
        import subprocess
        if shutil.which("git") is None or not (utils.PROJECT_ROOT / ".git").exists():
            self.skipTest("git repository not available")
        return subprocess.run(["git", *args], cwd=utils.PROJECT_ROOT, capture_output=True, text=True)

    def test_tracked_root_contains_only_three_directories(self):
        out = self._git("ls-files").stdout.splitlines()
        top_level = {line.split("/", 1)[0] for line in out if line}
        if not top_level:
            self.skipTest("no tracked files")
        self.assertLessEqual(top_level, {"scripts", "data", "sources"})

    def test_sources_env_is_ignored(self):
        res = self._git("check-ignore", "-q", "sources/.env")
        self.assertEqual(res.returncode, 0, "sources/.env must be git-ignored")

    def test_sources_env_is_not_tracked(self):
        res = self._git("ls-files", "--error-unmatch", "sources/.env")
        self.assertNotEqual(res.returncode, 0, "sources/.env must never be tracked")

    def test_generated_data_is_ignored(self):
        for path in ("data/extracted_images/0x1_0x2/r_1.jpg", "data/output/x.json",
                     "data/raw_data/serpapi/x_page001.json", "data/extracted_pages/x_page001.json"):
            res = self._git("check-ignore", "-q", path)
            self.assertEqual(res.returncode, 0, f"{path} must be git-ignored")

    def test_env_example_is_not_ignored(self):
        res = self._git("check-ignore", "-q", "scripts/.env.example")
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
