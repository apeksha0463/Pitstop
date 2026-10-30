"""
scripts/tests/test_washroom.py
==============================
Tests for the washroom review pipeline.

Covers:
  - extract_reviews --query / --topic-id: SerpApi params, num=20 on page 1,
    mutual exclusion, matched_by tags, review_id dedupe across searches,
    page-1 topics, washroom/ output folders, resuming from a saved token
  - load_saved_topics (0 credits)
  - collect_washroom_reviews: topic vs query plan, credit budget clamp,
    budget stop, quota stop, skipping completed restaurants, --refresh
  - extract_washroom_mentions: keyword matching (incl. false positives such
    as "rested" or "bathroom-themed"), snippet extraction, CSV rows

All tests run offline (fetch_page and the account check are mocked).
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
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
    from scripts import collect_washroom_reviews as collector
    from scripts import extract_reviews
    from scripts import extract_washroom_mentions as mentions
    from scripts import utils as _utils
except ImportError:
    import collect_washroom_reviews as collector
    import extract_reviews
    import extract_washroom_mentions as mentions
    import utils as _utils

DATA_ID = "0xAB:0xCD"
OTHER_ID = "0xEF:0x12"
PLACE = {"title": "Test Dhaba", "address": "Highway 1", "rating": 4.1, "reviews": 900, "type": "Restaurant"}


def review(rid, text, iso="2026-09-01T00:00:00Z", rating=3):
    return {"review_id": rid, "user": {"name": f"user {rid}"}, "rating": rating, "date": "1 month ago",
            "iso_date": iso, "snippet": text, "link": f"https://maps.example/{rid}"}


def page(reviews, next_token=None, topics=None):
    data = {"search_metadata": {"status": "Success"}, "place_info": PLACE, "reviews": reviews,
            "serpapi_pagination": {"next_page_token": next_token} if next_token else {}}
    if topics is not None:
        data["topics"] = topics
    return data


class _TempProject(unittest.TestCase):
    """Redirects every data/ path into a temporary project root and mocks fetch_page."""

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
            patch.object(extract_reviews.time, "sleep"),
            patch.object(collector.time, "sleep"),
        ]
        for p in self._patches:
            p.start()
        self.calls: list[dict] = []
        self.responses: dict = {}     # (filter value, page token or None) -> response | Exception
        self.default = page([])

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def fake_fetch(self, api_key, params, **kw):
        self.calls.append(dict(params))
        key = (params.get("query") or params.get("topic_id"), params.get("next_page_token"))
        item = self.responses.get((params.get("data_id"),) + key, self.responses.get(key, self.default))
        if isinstance(item, BaseException):
            raise item
        return item

    def extract(self, **kwargs):
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), \
                redirect_stdout(io.StringIO()):
            return extract_reviews.run_extraction("FAKE_KEY", DATA_ID, delay=0, **kwargs)


# ---------------------------------------------------------------------------
# extract_reviews: --query / --topic-id
# ---------------------------------------------------------------------------

class TestFilteredExtraction(_TempProject):

    def test_query_sent_with_num_20_on_first_page(self):
        self.responses[("washroom", None)] = page([review("r1", "Washroom clean")], next_token="t2")
        self.responses[("washroom", "t2")] = page([review("r2", "Toilet dirty")])
        self.extract(query="washroom", max_requests=5)
        self.assertEqual(self.calls[0]["query"], "washroom")
        self.assertEqual(self.calls[0]["num"], 20)
        self.assertNotIn("next_page_token", self.calls[0])
        self.assertEqual(self.calls[1]["query"], "washroom")      # filter kept on later pages
        self.assertEqual(self.calls[1]["next_page_token"], "t2")
        self.assertNotIn("topic_id", self.calls[0])

    def test_topic_id_sent_with_num_20(self):
        self.extract(topic_id="/m/abc", max_requests=1)
        self.assertEqual(self.calls[0]["topic_id"], "/m/abc")
        self.assertEqual(self.calls[0]["num"], 20)
        self.assertNotIn("query", self.calls[0])

    def test_unfiltered_first_page_has_no_num(self):
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), \
                redirect_stdout(io.StringIO()):
            extract_reviews.run_extraction("FAKE_KEY", DATA_ID, delay=0, max_requests=1,
                                           output_path=self.root / "out.json")
        self.assertNotIn("num", self.calls[0])

    def test_query_and_topic_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            self.extract(query="washroom", topic_id="/m/abc")
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                patch("sys.stderr", io.StringIO()):
            extract_reviews.parse_args(["--query", "washroom", "--topic-id", "/m/abc"])

    def test_cli_passes_filter_and_skips_place_lookup(self):
        with patch.object(extract_reviews, "run_cli", return_value={"collection_status": "completed"}) as cli, \
                redirect_stdout(io.StringIO()):
            extract_reviews.main(["--data-id", DATA_ID, "--query", "toilet"])
        kwargs = cli.call_args.kwargs
        self.assertEqual(kwargs["query"], "toilet")
        self.assertIsNone(kwargs["topic_id"])
        self.assertFalse(kwargs["restaurant_photos"])

    def test_reviews_tagged_and_saved_to_washroom_folder(self):
        self.responses[("washroom", None)] = page([review("r1", "Washroom clean")],
                                                  topics=[{"keyword": "clean washrooms", "id": "/m/w", "mentions": 4}])
        out = self.extract(query="washroom", max_requests=1)
        path = _utils.OUTPUT_DIR / "washroom" / "0xAB_0xCD_reviews.json"
        self.assertTrue(path.exists())
        self.assertEqual(out["reviews"][0]["matched_by"], ["query:washroom"])
        self.assertEqual(out["review_filter"], "query:washroom")
        self.assertEqual(out["topics"], [{"keyword": "clean washrooms", "id": "/m/w", "mentions": 4}])
        self.assertTrue((_utils.RAW_DATA_DIR / "serpapi" / "washroom" / "0xAB_0xCD_query-washroom_page001.json").exists())
        self.assertFalse((_utils.RAW_DATA_DIR / "serpapi" / "0xAB_0xCD_page001.json").exists())

    def test_dedupe_and_tags_merged_across_searches(self):
        shared = review("r1", "Washroom and toilet both clean")
        self.responses[("washroom", None)] = page([shared])
        self.responses[("toilet", None)] = page([shared, review("r2", "Toilet ok")])
        self.extract(query="washroom", max_requests=1)
        out = self.extract(query="toilet", max_requests=1)
        by_id = {r["review_id"]: r for r in out["reviews"]}
        self.assertEqual(len(out["reviews"]), 2)
        self.assertEqual(sorted(by_id["r1"]["matched_by"]), ["query:toilet", "query:washroom"])
        self.assertEqual(by_id["r2"]["matched_by"], ["query:toilet"])
        self.assertEqual([r["review_filter"] for r in out["filter_runs"]], ["query:washroom", "query:toilet"])

    def test_unfiltered_history_not_merged_into_washroom_file(self):
        _utils.save_json({"data_id": DATA_ID, "reviews": [{"review_id": "old", "text": "great food"}]},
                         _utils.OUTPUT_DIR / "Test_Dhaba_0xAB_0xCD.json")
        out = self.extract(query="washroom", max_requests=1)
        self.assertNotIn("old", [r["review_id"] for r in out["reviews"]])

    def test_resume_from_saved_token(self):
        self.responses[("toilet", "tok5")] = page([review("r9", "Toilet")])
        out = self.extract(query="toilet", max_requests=1, resume_token="tok5", first_page=5)
        self.assertEqual(self.calls[0]["next_page_token"], "tok5")
        self.assertEqual(self.calls[0]["num"], 20)
        self.assertTrue((_utils.RAW_DATA_DIR / "serpapi" / "washroom" / "0xAB_0xCD_query-toilet_page005.json").exists())
        self.assertEqual(out["filter_runs"][-1]["first_page"], 5)

    def test_failed_resume_keeps_token(self):
        self.responses[("toilet", "tok5")] = _utils.SerpApiError("boom")
        out = self.extract(query="toilet", max_requests=1, resume_token="tok5", first_page=5)
        self.assertEqual(out["next_page_token"], "tok5")

    def test_load_saved_topics_reads_raw_pages_without_requests(self):
        _utils.save_json(page([], topics=[{"keyword": "biryani", "id": "/m/b", "mentions": 9}]),
                         _utils.make_raw_page_path(DATA_ID, 1))
        _utils.save_json(page([], topics=[{"keyword": "biryani", "id": "/m/b", "mentions": 9},
                                          {"keyword": "restroom", "id": "/m/r", "mentions": 2}]),
                         _utils.make_raw_page_path(DATA_ID, 1, "query:toilet"))
        _utils.save_json(page([], topics=[{"keyword": "late page", "id": "/m/x"}]),
                         _utils.make_raw_page_path(DATA_ID, 2))
        topics = _utils.load_saved_topics(DATA_ID)
        self.assertEqual([t["id"] for t in topics], ["/m/b", "/m/r"])
        self.assertEqual(_utils.load_saved_topics(OTHER_ID), [])


# ---------------------------------------------------------------------------
# collect_washroom_reviews
# ---------------------------------------------------------------------------

RESTAURANTS = [{"restaurant": "Test Dhaba", "data_id": DATA_ID}, {"restaurant": "Other", "data_id": OTHER_ID}]


class TestCollector(_TempProject):

    def collect(self, restaurants=RESTAURANTS, budget=100, **kwargs):
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), \
                redirect_stdout(io.StringIO()):
            return collector.collect(restaurants, "FAKE_KEY", budget, delay=0, **kwargs)

    def test_queries_used_when_no_washroom_topic(self):
        _utils.save_json(page([], topics=[{"keyword": "biryani", "id": "/m/b"}]), _utils.make_raw_page_path(DATA_ID, 1))
        self.assertEqual(collector.plan_searches(DATA_ID, None),
                         ["query:washroom", "query:restroom", "query:toilet", "query:bathroom"])

    def test_washroom_topic_used_when_saved(self):
        _utils.save_json(page([], topics=[{"keyword": "clean toilets", "id": "/m/t", "mentions": 3},
                                          {"keyword": "rested", "id": "/m/z", "mentions": 50}]),
                         _utils.make_raw_page_path(DATA_ID, 1))
        self.assertEqual(collector.plan_searches(DATA_ID, None), ["topic:/m/t"])
        result = self.collect([RESTAURANTS[0]])
        self.assertEqual(result["credits_used"], 1)
        self.assertEqual(self.calls[0]["topic_id"], "/m/t")

    def test_breadth_first_one_page_per_search_then_more(self):
        self.responses[(DATA_ID, "washroom", None)] = page([review("r1", "washroom")], next_token="w2")
        self.responses[(DATA_ID, "washroom", "w2")] = page([review("r2", "washroom 2")])
        result = self.collect(max_pages=3)
        # round 1: 4 searches x 2 restaurants, round 2: the one with a next page
        self.assertEqual(result["credits_used"], 9)
        self.assertEqual(self.calls[-1].get("next_page_token"), "w2")
        self.assertEqual({r["status"] for r in result["restaurants"]}, {"complete"})

    def test_budget_stops_and_reports_pending(self):
        result = self.collect(budget=3)
        self.assertEqual(result["credits_used"], 3)
        self.assertEqual(len(self.calls), 3)
        self.assertIn("budget", result["stop_reason"])
        statuses = {r["data_id"]: r["status"] for r in result["restaurants"]}
        self.assertEqual(statuses, {DATA_ID: "incomplete", OTHER_ID: "not_started"})

    def test_rerun_resumes_and_skips_completed(self):
        self.collect(budget=3)
        self.calls.clear()
        result = self.collect(budget=100)
        self.assertEqual(result["credits_used"], 5)               # only the 5 missing searches
        self.assertEqual(self.calls[0]["query"], "bathroom")
        self.calls.clear()
        out = io.StringIO()
        with patch.object(extract_reviews, "fetch_page", side_effect=self.fake_fetch), redirect_stdout(out):
            result = collector.collect(RESTAURANTS, "FAKE_KEY", 100, delay=0)
        self.assertEqual(result["credits_used"], 0)
        self.assertEqual(self.calls, [])
        self.assertIn("Skipping 2", out.getvalue())

    def test_refresh_fetches_again(self):
        self.collect([RESTAURANTS[0]])
        self.calls.clear()
        self.assertEqual(self.collect([RESTAURANTS[0]])["credits_used"], 0)
        path = _utils.make_output_path(DATA_ID, review_filter="washroom")
        doc = _utils.load_json(path)
        for run in doc["filter_runs"]:
            run["run_at"] = "2000-01-01T00:00:00"     # saved before this --refresh run
        _utils.save_json(doc, path)
        result = self.collect([RESTAURANTS[0]], refresh=True)
        self.assertEqual(result["credits_used"], 4)
        self.assertEqual(result["restaurants"][0]["status"], "complete")

    def test_quota_exhausted_stops_everything(self):
        self.responses[(DATA_ID, "restroom", None)] = _utils.CreditsExhaustedError("no searches left")
        result = self.collect()
        self.assertEqual(len(self.calls), 2)
        self.assertIn("exhausted", result["stop_reason"])

    def test_api_error_skips_search_but_continues(self):
        self.responses[(DATA_ID, "restroom", None)] = _utils.SerpApiError("Bad request")
        result = self.collect()
        self.assertEqual(len(self.calls), 8)                       # not retried within the run
        statuses = {r["data_id"]: r["status"] for r in result["restaurants"]}
        self.assertEqual(statuses, {DATA_ID: "incomplete", OTHER_ID: "complete"})

    def test_page_limit_respected(self):
        self.responses[(DATA_ID, "washroom", None)] = page([review("r1", "washroom")], next_token="w2")
        result = self.collect([RESTAURANTS[0]], max_pages=1)
        self.assertEqual(result["credits_used"], 4)
        self.assertEqual(result["restaurants"][0]["status"], "complete_at_page_limit")

    def test_credit_budget_clamped_to_remaining(self):
        with patch.object(collector, "load_api_key", return_value="K" * 30), \
                patch.object(collector, "validate_api_key", return_value={"total_searches_left": 7}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(collector.check_credits(50), 7)
        with patch.object(collector, "load_api_key", return_value="K" * 30), \
                patch.object(collector, "validate_api_key", return_value={"total_searches_left": 700}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(collector.check_credits(50), 50)
        with patch.object(collector, "load_api_key", return_value="K" * 30), \
                patch.object(collector, "validate_api_key",
                             side_effect=_utils.CreditsExhaustedError("none left")), \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(collector.check_credits(50))


# ---------------------------------------------------------------------------
# extract_washroom_mentions
# ---------------------------------------------------------------------------

class TestKeywordMatching(unittest.TestCase):

    def test_all_listed_terms_match(self):
        for text in ["washroom", "Restroom", "TOILET", "bathroom", "loo", "lavatory", "wash room",
                     "rest room", "WC", "W.C.", "urinal", "hand wash area", "Washrooms", "toilets",
                     "loos", "lavatories", "urinals", "wash-room", "handwash area", "hand washing area"]:
            with self.subTest(text=text):
                self.assertTrue(mentions.is_washroom_related(f"The {text} was fine."))

    def test_common_misspellings(self):
        for text in ["washrom", "washroon", "wahsroom", "wasroom", "toliet", "toilette", "tiolet",
                     "restrom", "resroom", "bathrom", "lavatry", "lavatary"]:
            with self.subTest(text=text):
                self.assertTrue(mentions.is_washroom_related(f"{text} needs cleaning"))

    def test_false_positives(self):
        for text in ["We rested after a long drive.", "There was room for everyone.",
                     "Loved the bathroom-themed decor.", "Toilet humour on the menu walls.",
                     "toiletries", "Waterloo station", "loose parotta", "A tolet board outside",
                     "the rest, room was big", "The restaurant", "wcbc", "lavish buffet"]:
            with self.subTest(text=text):
                self.assertFalse(mentions.is_washroom_related(text))

    def test_bathroom_themed_does_not_hide_a_real_mention(self):
        self.assertTrue(mentions.is_washroom_related("Bathroom-themed decor, but the actual bathroom was dirty."))


class TestSnippetExtraction(unittest.TestCase):

    def test_only_washroom_sentences_kept(self):
        text = "Biryani was excellent. The washroom was filthy! Service was quick."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["The washroom was filthy!"])

    def test_unrelated_review(self):
        self.assertEqual(mentions.extract_washroom_snippets("Great food, rested well."), [])
        self.assertEqual(mentions.extract_washroom_snippets(None), [])

    def test_follow_on_sentence_and_separate_mentions(self):
        text = "Washroom is upstairs. It was very dirty. Paneer was great.\nToilets had no water"
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["Washroom is upstairs. It was very dirty.", "Toilets had no water"])

    def test_unrelated_follow_on_not_included(self):
        text = "Clean washrooms. The food was cold."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["Clean washrooms."])

    def test_long_run_on_sentence_trimmed_to_washroom_clause(self):
        text = ("food was great and staff were friendly and the paneer tikka was awesome and the naan was soft "
                "and the lassi was thick and sweet and the dal was rich and creamy, the washroom however was "
                "really dirty and smelly, but overall a good place to stop on the highway for families")
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["the washroom however was really dirty and smelly"])

    def test_dots_without_space_end_a_sentence(self):
        text = "Too expensive and food is worst then street food taste...washroom is not clean.."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["washroom is not clean.."])

    def test_ellipsis_character_ends_a_sentence(self):
        text = "Food taste is out of the world…cleaning and hygiene in restroom was great"
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["cleaning and hygiene in restroom was great"])
        text = "Just stopped by for dinner… staff uses washroom to do video calls…food was pathetic"
        self.assertEqual(mentions.extract_washroom_snippets(text), ["staff uses washroom to do video calls…"])

    def test_stop_glued_to_next_sentence_ends_it(self):
        text = "Dosa was not cooked properly .Coming to hygiene the wash rooms were dirty.Service was slow"
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["Coming to hygiene the wash rooms were dirty."])
        text = "Enough parking facility.Must say Washroom is also clean.I can't say anything about the food"
        self.assertEqual(mentions.extract_washroom_snippets(text), ["Must say Washroom is also clean."])

    def test_ellipsis_followed_by_dot_and_closing_bracket_end_a_sentence(self):
        text = "menu n place immersive food pathetic….staff uses washroom for calls…."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["staff uses washroom for calls…."])
        text = "Washroom is clean ( rare on the NH .) Outside there is a mini garden."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["Washroom is clean ( rare on the NH .)"])

    def test_long_question_does_not_take_the_next_sentence(self):
        text = ("Who would want to go next to a smelly toilet after a meal to wash their hands? "
                "To the management, the food was cold.")
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["Who would want to go next to a smelly toilet after a meal to wash their hands?"])

    def test_initials_do_not_split(self):
        self.assertEqual(mentions.extract_washroom_snippets("Food good. The W.C was dirty."),
                         ["The W.C was dirty."])

    def test_lead_in_sentence_takes_the_next_one(self):
        text = "Great food.\nParking/Restroom:\nHuge parking, restrooms kept clean.\nStaff polite."
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["Parking/Restroom: Huge parking, restrooms kept clean."])
        text = "Lots of shops. And the restrooms? Spotless. Coffee was cold."
        self.assertEqual(mentions.extract_washroom_snippets(text), ["And the restrooms? Spotless."])

    def test_food_clauses_dropped_from_medium_sentence(self):
        text = ("A good place to have food with family & friends far from the city crowd (at least till now), "
                "focused on North Indian food, nice hygienic with clean washroom.")
        self.assertEqual(mentions.extract_washroom_snippets(text), ["nice hygienic with clean washroom."])

    def test_relative_clause_kept_with_washroom(self):
        text = ("The only disappointment was the bathroom, which was not clean, and some of the plumbing work "
                "seemed unfinished.")
        self.assertEqual(mentions.extract_washroom_snippets(text),
                         ["The only disappointment was the bathroom which was not clean"])

    def test_snippet_is_plain_text(self):
        snippet = mentions.extract_washroom_snippets("Restroom   was\tclean.")[0]
        self.assertEqual(snippet, "Restroom was clean.")

    def test_translated_review_keeps_english_part(self):
        text = "(Translated by Google) Toilet is clean.\n\n(Original)\nशौचालय साफ है।"
        self.assertEqual(mentions.extract_washroom_snippets(text), ["Toilet is clean."])


class TestMentionOutputs(_TempProject):

    def test_rows_and_summary(self):
        _utils.save_json({"data_id": DATA_ID, "reviews": [
            _utils.flatten_review(review("r1", "Food ok. Washroom dirty. Great lassi. Toilet had no water.",
                                         iso="2026-08-01T00:00:00Z", rating=2)),
            _utils.flatten_review(review("r2", "Lovely food.")),
        ]}, _utils.OUTPUT_DIR / "washroom" / "0xAB_0xCD_reviews.json")
        # Same review also in the unfiltered output: counted once
        _utils.save_json({"data_id": DATA_ID, "reviews": [
            _utils.flatten_review(review("r1", "Food ok. Washroom dirty. Great lassi. Toilet had no water.")),
            _utils.flatten_review(review("r3", "Clean restrooms.", iso="2026-10-01T00:00:00Z", rating=5)),
        ]}, _utils.OUTPUT_DIR / "0xAB_0xCD_reviews.json")
        _utils.save_json({"saved_at": "x"}, _utils.OUTPUT_DIR / "washroom" / "collection_summary.json")

        csv_path = self.root / "restaurants.csv"
        csv_path.write_text("#,Restaurant,data_id\n1,Test Dhaba,0xAB:0xCD\n2,Other,0xEF:0x12\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            mentions.main(["--csv", str(csv_path)])

        with open(_utils.PROCESSED_DIR / "washroom_mentions.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0]), mentions.MENTION_COLUMNS)
        # One row per review: r1's two separate washroom passages are joined, not counted twice
        self.assertEqual([r["washroom_snippet"] for r in rows],
                         ["Clean restrooms.", "Washroom dirty. … Toilet had no water."])
        self.assertEqual([r["mention_id"] for r in rows], ["r3", "r1"])
        self.assertEqual([r["passage_count"] for r in rows], ["1", "2"])
        self.assertEqual(rows[1]["star_rating"], "2")
        self.assertEqual(rows[1]["restaurant"], "Test Dhaba")

        with open(_utils.PROCESSED_DIR / "washroom_summary.csv", encoding="utf-8", newline="") as fh:
            summary = {r["data_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(summary[DATA_ID]["reviews_collected"], "3")
        self.assertEqual(summary[DATA_ID]["washroom_related_reviews"], "2")
        self.assertEqual(summary[DATA_ID]["latest_mention_date"], "2026-10-01T00:00:00Z")
        self.assertEqual(summary[OTHER_ID]["reviews_collected"], "0")
        self.assertEqual(summary[OTHER_ID]["latest_mention_date"], "")


if __name__ == "__main__":
    unittest.main()
