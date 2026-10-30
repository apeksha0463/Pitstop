"""
scripts/tests/test_merge_reviews.py
===================================
Tests for merge_reviews: one entry per review across SerpApi (unfiltered and
washroom searches) and Apify, source tags, filling missing fields, date
normalisation, the archive folder being ignored, places outside the list
left out, and the per-restaurant files and CSV.

All tests run offline.
"""

from __future__ import annotations

import csv
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import merge_reviews as merge
    from scripts import utils as _utils
    from scripts.tests.test_washroom import DATA_ID, OTHER_ID, _TempProject
except ImportError:
    import merge_reviews as merge
    import utils as _utils
    from tests.test_washroom import DATA_ID, OTHER_ID, _TempProject

REST = [{"restaurant": "Test Dhaba", "data_id": DATA_ID, "google_review_count": "900"}]


def rev(rid, text="Nice", iso="2026-10-01T10:00:00Z", **extra):
    return {"review_id": rid, "text": text, "iso_date": iso, "rating": 4.0, **extra}


class TestMerge(_TempProject):

    def save(self, folder, reviews, data_id=DATA_ID, **extra):
        _utils.save_json({"data_id": data_id, "reviews": reviews, **extra}, folder / f"{data_id[:4]}_reviews.json")

    def test_one_entry_per_review_with_every_source(self):
        out = _utils.OUTPUT_DIR
        self.save(out / "washroom", [rev("r1", "Washroom dirty", matched_by=["query:washroom"])])
        self.save(out, [rev("r1", "Washroom dirty", author="A"), rev("r2")])
        self.save(out / "apify", [rev("r1", "Washroom dirty", iso="2026-10-01T10:00:00.123Z",
                                      review_link="https://x/r1"), rev("r3", iso="2026-10-05T00:00:00.000Z")])
        merged = merge.load_sources()[DATA_ID]
        self.assertEqual([r["review_id"] for r in merged], ["r3", "r1", "r2"])     # newest first
        r1 = next(r for r in merged if r["review_id"] == "r1")
        self.assertEqual(r1["sources"], ["serpapi_washroom_search", "serpapi", "apify"])
        self.assertEqual(r1["matched_by"], ["query:washroom"])
        self.assertEqual(r1["author"], "A")                       # filled from SerpApi
        self.assertEqual(r1["review_link"], "https://x/r1")       # filled from Apify
        self.assertEqual(r1["iso_date"], "2026-10-01T10:00:00Z")  # normalised
        self.assertEqual(merged[0]["iso_date"], "2026-10-05T00:00:00Z")

    def test_filtered_file_outside_the_washroom_folder_is_tagged_as_search(self):
        self.save(_utils.OUTPUT_DIR, [rev("r1")], review_filter="query:toilet")
        self.assertEqual(merge.load_sources()[DATA_ID][0]["sources"], ["serpapi_washroom_search"])

    def test_archive_folder_is_not_read(self):
        self.save(_utils.OUTPUT_DIR / "archive", [rev("old")])
        self.assertEqual(merge.load_sources(), {})

    def test_files_and_csv_only_for_listed_restaurants(self):
        self.save(_utils.OUTPUT_DIR, [rev("r1", "Line one\nline two", author="Private Name")])
        self.save(_utils.OUTPUT_DIR / "apify", [rev("x1")], data_id=OTHER_ID)
        with redirect_stdout(io.StringIO()):
            merged = merge.build(REST)
        self.assertEqual(list(merged), [DATA_ID])
        doc = _utils.load_json(merge.merged_dir() / "0xAB_0xCD_reviews.json")
        self.assertEqual((doc["reviews_total"], doc["reviews_by_source"]["serpapi"], doc["reviews_by_source"]["apify"]),
                         (1, 1, 0))
        self.assertEqual(sorted(p.name for p in merge.merged_dir().iterdir()), ["0xAB_0xCD_reviews.json"])
        with open(_utils.PROCESSED_DIR / "all_reviews.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0]), merge.CSV_COLUMNS)
        self.assertEqual((rows[0]["review_id"], rows[0]["sources"], rows[0]["text"]), ("r1", "serpapi", "Line one line two"))
        self.assertNotIn("Private Name", str(rows))


if __name__ == "__main__":
    unittest.main()
