"""
tests/test_paths.py
====================
Tests for path utilities in scripts/utils.py.

Covers:
  - PROJECT_ROOT resolves to the project root (not scripts/)
  - All data subdirectory constants are children of data/
  - OUTPUT_DIR is inside the project root
  - make_output_path returns a Path inside OUTPUT_DIR
  - make_output_path embeds the safe data_id and a timestamp
  - make_output_path with a restaurant name produces a friendlier filename
  - make_raw_page_path returns a Path inside EXTRACTED_PAGES_DIR
  - save_json creates intermediate directories automatically
  - ensure_dirs creates all required directories
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import utils


class TestProjectRoot(unittest.TestCase):

    def test_project_root_is_not_scripts_dir(self):
        self.assertNotEqual(utils.PROJECT_ROOT.name, "scripts")

    def test_project_root_contains_requirements_txt(self):
        self.assertTrue((utils.PROJECT_ROOT / "requirements.txt").exists())

    def test_data_dir_is_child_of_project_root(self):
        self.assertEqual(utils.DATA_DIR.parent, utils.PROJECT_ROOT)

    def test_output_dir_is_child_of_project_root(self):
        self.assertEqual(utils.OUTPUT_DIR.parent, utils.PROJECT_ROOT)


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

    def test_restaurant_name_included_when_given(self):
        path = self._run("0xABCD:0x1234", "Swathi Delicacy")
        self.assertIn("Swathi", path.name)

    def test_no_two_paths_identical_for_same_input(self):
        """Two consecutive calls should not collide (timestamp differs)."""
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_output = Path(tmpdir)
            with patch.object(utils, "OUTPUT_DIR", fake_output):
                p1 = utils.make_output_path("0xABCD:0x1234")
                time.sleep(1)   # ensure different timestamp
                p2 = utils.make_output_path("0xABCD:0x1234")
        self.assertNotEqual(p1, p2)


class TestMakeRawPagePath(unittest.TestCase):

    def test_returns_path_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "EXTRACTED_PAGES_DIR", Path(tmpdir)):
                path = utils.make_raw_page_path("0xABCD:0x1234", 1)
        self.assertIsInstance(path, Path)

    def test_page_number_zero_padded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(utils, "EXTRACTED_PAGES_DIR", Path(tmpdir)):
                path = utils.make_raw_page_path("0xABCD:0x1234", 5)
        self.assertIn("page005", path.name)

    def test_path_in_extracted_pages_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_pages = Path(tmpdir) / "extracted_pages"
            with patch.object(utils, "EXTRACTED_PAGES_DIR", fake_pages):
                path = utils.make_raw_page_path("0xABCD:0x1234", 1)
        self.assertEqual(path.parent, fake_pages)


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
            "likes", "owner_response", "photos_count", "photos",
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


if __name__ == "__main__":
    print("Running path & utility tests...\n")
    unittest.main(verbosity=2)
