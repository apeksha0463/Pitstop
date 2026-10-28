"""
scripts/tests/test_extract_pages.py
===================================
Tests for scripts/extract_pages.py — raw page persistence.

Covers:
  - save_page writes a readable JSON file
  - load_page returns the original dict
  - list_saved_pages returns files in sorted order
  - iter_saved_pages yields dicts
  - count_saved_pages returns correct count
  - Missing file raises FileNotFoundError
  - Corrupt file raises ValueError
"""

from __future__ import annotations

import json
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
    import scripts.extract_pages as ep
    from scripts.utils import EXTRACTED_PAGES_DIR
except ImportError:
    import extract_pages as ep
    from utils import EXTRACTED_PAGES_DIR


class TestSavePage(unittest.TestCase):

    def test_save_creates_file(self):
        data = {"reviews": [{"id": "r1"}], "serpapi_pagination": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                path = ep.save_page("0xABCD:0x1234", 1, data)
                self.assertTrue(path.exists())

    def test_saved_content_is_readable_json(self):
        data = {"reviews": [{"id": "r1"}], "source": "test"}
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                path = ep.save_page("0xABCD:0x1234", 1, data)
                loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["source"], "test")

    def test_filename_uses_safe_id_and_page_number(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                path = ep.save_page("0xABCD:0x1234", 7, {})
                self.assertIn("page007", path.name)


class TestLoadPage(unittest.TestCase):

    def test_load_returns_dict(self):
        data = {"key": "value", "nested": {"a": 1}}
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "test_page.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            result = ep.load_page(p)
        self.assertEqual(result, data)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ep.load_page(Path("/nonexistent/path/page.json"))

    def test_load_corrupt_file_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "bad.json"
            p.write_text("{ not valid json }", encoding="utf-8")
            with self.assertRaises(ValueError):
                ep.load_page(p)


class TestListAndCount(unittest.TestCase):

    def test_list_returns_sorted_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                ep.save_page("0xAABB:0xCCDD", 3, {})
                ep.save_page("0xAABB:0xCCDD", 1, {})
                ep.save_page("0xAABB:0xCCDD", 2, {})
                pages = ep.list_saved_pages("0xAABB:0xCCDD")
            names = [p.name for p in pages]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 3)

    def test_count_matches_saved_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                ep.save_page("0x1111:0x2222", 1, {})
                ep.save_page("0x1111:0x2222", 2, {})
                count = ep.count_saved_pages("0x1111:0x2222")
        self.assertEqual(count, 2)

    def test_list_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                pages = ep.list_saved_pages("0xNONEXISTENT")
        self.assertEqual(pages, [])

    def test_iter_saved_pages_yields_dicts(self):
        payload = {"data_id": "x", "page": 1}
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ep, "EXTRACTED_PAGES_DIR", fake_dir):
                ep.save_page("0xITER:0xTEST", 1, payload)
                results = list(ep.iter_saved_pages("0xITER:0xTEST"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data_id"], "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
