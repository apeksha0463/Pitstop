"""
scripts/tests/test_collect_latest_reviews.py
============================================
Tests for collect_latest_reviews: breadth-first pages, the credit budget,
resuming from a saved page token, reusing earlier raw pages, pagination
that ends early (one no_cache retry) and the coverage statuses.

All tests run offline (fetch_page is mocked).
"""

from __future__ import annotations

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
    from scripts import collect_latest_reviews as latest
    from scripts import extract_reviews
    from scripts import utils as _utils
    from scripts.tests.test_washroom import DATA_ID, OTHER_ID, _TempProject, page, review
except ImportError:
    import collect_latest_reviews as latest
    import extract_reviews
    import utils as _utils
    from tests.test_washroom import DATA_ID, OTHER_ID, _TempProject, page, review


def reviews(prefix, n):
    return [review(f"{prefix}{i}", f"review {prefix}{i}") for i in range(n)]


BIG = {"restaurant": "Test Dhaba", "data_id": DATA_ID, "google_review_count": "900"}
SMALL = {"restaurant": "Other", "data_id": OTHER_ID, "google_review_count": "5"}


class TestLatestCollector(_TempProject):

    def setUp(self):
        super().setUp()
        p = patch.object(latest.time, "sleep")
        p.start()
        self._patches.append(p)

    def collect(self, restaurants=(BIG, SMALL), budget=100, target=30):
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), \
                redirect_stdout(io.StringIO()):
            return latest.collect(list(restaurants), "FAKE_KEY", budget, target, delay=0)

    def coverage(self, restaurants=(BIG, SMALL), target=30):
        with redirect_stdout(io.StringIO()):
            rows = latest.write_report(list(restaurants), target)
        return {r["data_id"]: r for r in rows}

    def test_breadth_first_until_target(self):
        self.responses[(DATA_ID, None, None)] = page(reviews("a", 10), next_token="a2")
        self.responses[(DATA_ID, None, "a2")] = page(reviews("b", 20), next_token="a3")
        self.responses[(OTHER_ID, None, None)] = page(reviews("s", 5))
        result = self.collect()
        self.assertEqual(result["credits_used"], 3)
        # page 1 of both restaurants first, then page 2 of the one that needs it
        self.assertEqual([(c["data_id"], c.get("next_page_token")) for c in self.calls],
                         [(DATA_ID, None), (OTHER_ID, None), (DATA_ID, "a2")])
        self.assertEqual(self.calls[2]["num"], _utils.REVIEWS_PER_PAGE)
        cov = self.coverage()
        self.assertEqual((cov[DATA_ID]["reviews_downloaded"], cov[DATA_ID]["status"]), (30, latest.TARGET_MET))
        # Only 5 reviews exist: every one was downloaded
        self.assertEqual(cov[OTHER_ID]["status"], latest.COMPLETE)

    def test_budget_stop_then_resume_from_saved_token(self):
        self.responses[(DATA_ID, None, None)] = page(reviews("a", 10), next_token="a2")
        self.responses[(DATA_ID, None, "a2")] = page(reviews("b", 20), next_token="a3")
        result = self.collect(restaurants=(BIG,), budget=1)
        self.assertIn("budget", result["stop_reason"])
        self.assertEqual(self.coverage((BIG,))[DATA_ID]["status"], latest.IN_PROGRESS)
        self.calls.clear()
        self.collect(restaurants=(BIG,))
        self.assertEqual([c.get("next_page_token") for c in self.calls], ["a2"])   # page 1 not paid for again
        self.assertEqual(self.coverage((BIG,))[DATA_ID]["status"], latest.TARGET_MET)

    def test_raising_the_target_continues_where_it_stopped(self):
        self.responses[(DATA_ID, None, None)] = page(reviews("a", 10), next_token="a2")
        self.responses[(DATA_ID, None, "a2")] = page(reviews("b", 20), next_token="a3")
        self.responses[(DATA_ID, None, "a3")] = page(reviews("c", 20), next_token="a4")
        self.collect(restaurants=(BIG,), target=30)
        self.calls.clear()
        self.collect(restaurants=(BIG,), target=50)
        self.assertEqual([c.get("next_page_token") for c in self.calls], ["a3"])
        self.assertEqual(self.coverage((BIG,), target=50)[DATA_ID]["reviews_downloaded"], 50)

    def test_earlier_raw_pages_are_reused(self):
        _utils.save_json(page(reviews("a", 10), next_token="old2"), _utils.make_raw_page_path(DATA_ID, 1))
        progress = latest.load_progress([BIG])
        self.assertEqual(progress[DATA_ID], {"next_page": 2, "next_page_token": "old2", "finished": False})

    def test_pagination_ending_early_is_only_retried_on_request(self):
        # Google reports 900 reviews but SerpApi stops after 5
        self.responses[(DATA_ID, None, None)] = page(reviews("a", 5))
        self.collect(restaurants=(BIG,))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.coverage((BIG,))[DATA_ID]["status"], latest.ENDED_EARLY)
        self.calls.clear()
        self.collect(restaurants=(BIG,))
        self.assertEqual(self.calls, [])   # no credits spent on it again by default
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), \
                redirect_stdout(io.StringIO()):
            latest.collect([BIG], "FAKE_KEY", 100, 30, delay=0, retry_ended_early=True)
        # Retried once from page 1 without SerpApi's cache, not in a loop
        self.assertEqual([c.get("no_cache") for c in self.calls], ["true"])

    def test_restart_from_page_one_keeps_the_old_raw_pages(self):
        old = page(reviews("old", 10), next_token="stale")
        _utils.save_json(old, _utils.make_raw_page_path(DATA_ID, 1))
        progress = {"next_page": 1, "next_page_token": None, "finished": False}
        self.responses[(DATA_ID, None, None)] = page(reviews("new", 10), next_token="n2")
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch):
            latest.fetch_one_page("FAKE_KEY", BIG, progress)
        archived = list((_utils.RAW_DATA_DIR / "serpapi" / "superseded").glob("*_page001_*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(_utils.load_json(archived[0])["reviews"][0]["review_id"], "old0")
        self.assertEqual(_utils.load_json(_utils.make_raw_page_path(DATA_ID, 1))["reviews"][0]["review_id"], "new0")

    def test_run_stops_when_pages_keep_ending_early(self):
        many = [{"restaurant": f"R{i}", "data_id": f"0x{i}:0x{i}", "google_review_count": "900"} for i in range(6)]
        self.default = page(reviews("x", 5))
        result = self.collect(restaurants=many)
        self.assertEqual(len(self.calls), latest.MAX_EARLY_ENDS_IN_A_ROW)
        self.assertIn("truncated", result["stop_reason"])

    def test_expired_token_restarts_from_page_one(self):
        _utils.save_json(page(reviews("a", 10), next_token="stale"), _utils.make_raw_page_path(DATA_ID, 1))
        self.responses[(DATA_ID, None, "stale")] = _utils.SerpApiError("Invalid next_page_token")
        result = self.collect(restaurants=(BIG,))
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(latest.load_progress([BIG])[DATA_ID]["next_page"], 1)

    def test_quota_exhausted_stops_the_run(self):
        self.responses[(DATA_ID, None, None)] = _utils.CreditsExhaustedError("out of searches")
        result = self.collect()
        self.assertEqual(result["stop_reason"], "SerpApi credits exhausted.")
        self.assertEqual(len(self.calls), 1)

    def test_credit_estimates(self):
        fresh = {"next_page": 1, "next_page_token": None, "finished": False}
        # page 1 holds 10 reviews, later pages 20: 100 reviews = 1 + 5 pages
        self.assertEqual(latest.credits_to_target(0, 100, 900, fresh), 6)
        self.assertEqual(latest.credits_to_target(0, 100, 30, fresh), 2)
        # Reviews saved without a resumable token still need a restart from page 1
        self.assertEqual(latest.credits_to_target(98, 100, 900, fresh), 6)
        resumable = {"next_page": 3, "next_page_token": "t", "finished": False}
        self.assertEqual(latest.credits_to_target(30, 100, 900, resumable), 4)


if __name__ == "__main__":
    unittest.main()
