"""
scripts/tests/test_score_washroom_sentiment.py
==============================================
Tests for score_washroom_sentiment with a fake scorer (no model download):
aspect choice, multi-passage averaging, needs_review flags, the per-restaurant
summary (counts, recent window, low_data) and the output files.

All tests run offline.
"""

from __future__ import annotations

import csv
import datetime
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
    from scripts import score_washroom_sentiment as sent
    from scripts import utils as _utils
    from scripts.extract_washroom_mentions import MENTION_COLUMNS
    from scripts.tests.test_washroom import DATA_ID, OTHER_ID, _TempProject
except ImportError:
    import score_washroom_sentiment as sent
    import utils as _utils
    from extract_washroom_mentions import MENTION_COLUMNS
    from tests.test_washroom import DATA_ID, OTHER_ID, _TempProject


def fake_scorer(calls=None):
    """Negative for 'dirty', positive for 'clean', unsure for 'okay', else neutral."""
    def score(texts, aspects):
        if calls is not None:
            calls.extend(zip(texts, aspects))
        out = []
        for t in texts:
            t = t.lower()
            out.append([0.9, 0.05, 0.05] if "dirty" in t else [0.05, 0.05, 0.9] if "clean" in t
                       else [0.3, 0.4, 0.3] if "okay" in t else [0.1, 0.8, 0.1])
        return out
    return score


def mention(rid, snippet, data_id=DATA_ID, iso="2026-09-01T00:00:00Z"):
    return {**{c: "" for c in MENTION_COLUMNS}, "mention_id": rid, "review_id": rid, "data_id": data_id,
            "restaurant": "Test Dhaba", "washroom_snippet": snippet, "iso_date": iso}


class TestScoring(unittest.TestCase):

    def test_aspect_is_the_word_the_reviewer_used(self):
        calls = []
        sent.score_mentions([mention("r1", "The Restroom was clean."), mention("r2", "Nice loo")], fake_scorer(calls))
        self.assertEqual([a for _, a in calls], ["restroom", "loo"])
        self.assertEqual(sent.aspect_of("no washroom word here"), "washroom")

    def test_labels_scores_and_confidence(self):
        rows = sent.score_mentions([mention("r1", "Washroom dirty."), mention("r2", "Clean toilets.")], fake_scorer())
        self.assertEqual([r["sentiment"] for r in rows], ["negative", "positive"])
        self.assertEqual([r["sentiment_score"] for r in rows], [-0.85, 0.85])
        self.assertEqual(rows[0]["confidence"], 0.9)
        self.assertFalse(rows[0]["needs_review"])

    def test_passages_are_scored_separately_and_disagreement_is_flagged(self):
        calls = []
        row = sent.score_mentions([mention("r1", "Toilet was dirty. … Other branches have clean toilets.")],
                                  fake_scorer(calls))[0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(row["sentiment_score"], 0.0)
        self.assertTrue(row["needs_review"])

    def test_low_confidence_is_flagged(self):
        row = sent.score_mentions([mention("r1", "Washroom is okay.")], fake_scorer())[0]
        self.assertEqual(row["sentiment"], "neutral")
        self.assertTrue(row["needs_review"])


class TestSummary(unittest.TestCase):

    def test_summary_counts_recent_window_and_low_data(self):
        rows = sent.score_mentions([
            mention("r1", "Washroom dirty.", iso="2026-10-01T00:00:00Z"),
            mention("r2", "Clean washroom.", iso="2026-05-01T00:00:00Z"),
            mention("r3", "Clean washroom.", iso="2020-01-01T00:00:00Z"),
        ], fake_scorer())
        restaurants = [{"restaurant": "Test Dhaba", "data_id": DATA_ID}, {"restaurant": "Other", "data_id": OTHER_ID}]
        summary = sent.summarise(restaurants, rows, today=datetime.date(2026, 10, 30))
        s = summary[0]
        self.assertEqual((s["washroom_reviews"], s["positive"], s["negative"]), (3, 2, 1))
        self.assertEqual(s["mean_sentiment_score"], round((-0.85 + 0.85 + 0.85) / 3, 3))
        self.assertEqual(s["share_negative"], 0.333)
        self.assertEqual((s["recent_reviews"], s["recent_mean_sentiment_score"]), (2, 0.0))   # 2020 is too old
        self.assertTrue(s["low_data"])
        self.assertEqual((summary[1]["washroom_reviews"], summary[1]["mean_sentiment_score"]), (0, ""))


class TestFiles(_TempProject):

    def test_main_writes_both_csvs(self):
        path = _utils.PROCESSED_DIR / "washroom_mentions.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=MENTION_COLUMNS)
            writer.writeheader()
            writer.writerows([mention("r1", "Washroom dirty."), mention("r2", "Clean washroom.")])
        csv_path = self.root / "restaurants.csv"
        csv_path.write_text("#,Restaurant,data_id\n1,Test Dhaba,0xAB:0xCD\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sent.main(["--csv", str(csv_path)], scorer=fake_scorer()), 0)
        with open(_utils.PROCESSED_DIR / "washroom_mentions_scored.csv", encoding="utf-8", newline="") as fh:
            scored = list(csv.DictReader(fh))
        self.assertEqual(list(scored[0]), sent.SCORED_COLUMNS)
        self.assertEqual([r["sentiment"] for r in scored], ["negative", "positive"])
        with open(_utils.PROCESSED_DIR / "washroom_sentiment_summary.csv", encoding="utf-8", newline="") as fh:
            summary = list(csv.DictReader(fh))
        self.assertEqual(list(summary[0]), sent.SUMMARY_COLUMNS)
        self.assertEqual(summary[0]["washroom_reviews"], "2")

    def test_missing_mentions_file(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sent.main([], scorer=fake_scorer()), 1)


if __name__ == "__main__":
    unittest.main()
