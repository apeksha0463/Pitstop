"""
scripts/tests/test_upload_to_supabase.py
========================================
Offline, mocked tests for scripts/upload_to_supabase.py: file-to-row conversion,
batching, upsert keys and table order, washroom grade thresholds, the count check,
image-to-review mapping, and a guard that no file in site/ holds a secret key.
Any network connection attempted while these tests run fails the test.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import upload_to_supabase as up
except ImportError:
    import upload_to_supabase as up


def _no_network(*args, **kwargs):
    raise AssertionError("tests must not touch the network")


_guards = [patch.object(socket.socket, "connect", _no_network), patch.object(socket, "create_connection", _no_network)]


def setUpModule():
    for g in _guards:
        g.start()


def tearDownModule():
    for g in _guards:
        g.stop()


REVIEW_CSV_ROW = {"restaurant": "A", "data_id": "0x1:0x2", "review_id": "r1", "iso_date": "2026-10-29T18:21:40Z",
                  "rating": "5.0", "sources": "serpapi;apify", "matched_by": "query:toilet;query:washroom",
                  "language": "", "translated": "True", "text": "", "review_link": "https://x"}
MENTION_CSV_ROW = {"mention_id": "m1", "restaurant": "A", "data_id": "0x1:0x2", "review_id": "r1",
                   "review_date": "a year ago", "iso_date": "2025-10-06T14:25:54Z", "star_rating": "1.0",
                   "washroom_snippet": "Washroom smells.", "passage_count": "1", "full_review_text": "x",
                   "review_link": "", "aspect": "washroom", "sentiment": "negative", "sentiment_score": "-0.996",
                   "confidence": "0.997", "p_negative": "0.997", "p_neutral": "0.001", "p_positive": "0.001",
                   "needs_review": "False"}


def summary_pair(data_id="0x1:0x2", reviews="6", mean="0.3", low="False"):
    s = {"restaurant": "A", "data_id": data_id, "google_review_count": "197", "sample_reviews": "100",
         "sample_reviews_with_text": "80", "washroom_in_sample": "2", "washroom_share": "0.025",
         "washroom_search_reviews": "4", "washroom_related_reviews": "6", "latest_mention_date": ""}
    t = {"restaurant": "A", "data_id": data_id, "washroom_reviews": reviews, "positive": "4", "neutral": "1",
         "negative": "1", "mean_sentiment_score": mean, "share_negative": "", "recent_reviews": "0",
         "recent_mean_sentiment_score": "", "needs_review": "0", "latest_mention_date": "", "low_data": low}
    return s, t


class TestConversion(unittest.TestCase):

    def test_review_row(self):
        row = up.review_row(REVIEW_CSV_ROW)
        self.assertEqual(row["sources"], ["serpapi", "apify"])
        self.assertEqual(row["matched_by"], ["query:toilet", "query:washroom"])
        self.assertIs(row["translated"], True)
        self.assertEqual(row["rating"], 5.0)
        self.assertIsNone(row["text"])        # empty -> null
        self.assertIsNone(row["language"])
        self.assertNotIn("restaurant", row)   # no names of any kind beyond the keys

    def test_review_without_matches_gets_empty_arrays(self):
        row = up.review_row({**REVIEW_CSV_ROW, "sources": "apify", "matched_by": "", "translated": "False"})
        self.assertEqual((row["sources"], row["matched_by"], row["translated"]), (["apify"], [], False))

    def test_mention_row_keeps_every_column_typed(self):
        row = up.mention_row(MENTION_CSV_ROW)
        self.assertEqual(set(row), set(MENTION_CSV_ROW))
        self.assertEqual(row["sentiment_score"], -0.996)
        self.assertEqual(row["passage_count"], 1)
        self.assertIs(row["needs_review"], False)
        self.assertIsNone(row["review_link"])

    def test_summary_rows_join_both_files(self):
        s, t = summary_pair()
        (row,) = up.summary_rows([s], [t])
        self.assertEqual(row["google_review_count"], 197)
        self.assertEqual(row["washroom_reviews"], 6)
        self.assertEqual(row["washroom_share"], 0.025)
        self.assertIsNone(row["share_negative"])
        self.assertIs(row["low_data"], False)
        self.assertNotIn("restaurant", row)

    def test_summary_without_sentiment_row_is_none_and_low_data(self):
        s, _ = summary_pair()
        (row,) = up.summary_rows([s], [])
        self.assertEqual((row["washroom_reviews"], row["grade"], row["low_data"]), (0, "none", True))

    def test_restaurant_row_side_and_detour(self):
        st = {"data_id": "0x1:0x2", "name": "A", "rating": "4.5", "google_reviews": 10, "cuisine": "Veg",
              "position": "Short detour", "lat": 13.1, "lng": 77.6, "along_m": 12345.678, "dist_m": 1234.5,
              "side": "right", "detour": True, "maps_url": "https://m", "position_note": ""}
        (row,) = up.restaurant_rows([st], [{"data_id": "0x1:0x2", "Location": "NH44"}])
        self.assertEqual((row["route_km"], row["side"], row["detour_km"]), (12.35, "east", 1.23))
        self.assertEqual((row["location"], row["google_rating"], row["position_note"]), ("NH44", 4.5, None))
        (row,) = up.restaurant_rows([{**st, "side": "left", "detour": False}], [])
        self.assertEqual((row["side"], row["detour_km"], row["location"]), ("west", None, None))


class TestGradeThresholds(unittest.TestCase):

    def grade(self, mean, reviews="6"):
        s, t = summary_pair(reviews=reviews, mean=mean)
        return up.summary_rows([s], [t])[0]["grade"]

    def test_thresholds_match_the_route_map(self):
        self.assertEqual(self.grade("0.3"), "good")
        self.assertEqual(self.grade("0.2999"), "moderate")
        self.assertEqual(self.grade("-0.3"), "moderate")
        self.assertEqual(self.grade("-0.3001"), "bad")

    def test_no_reviews_or_no_mean_is_none(self):
        self.assertEqual(self.grade("0.9", reviews="0"), "none")
        self.assertEqual(self.grade(""), "none")


class TestBatching(unittest.TestCase):

    def test_batches_of_500(self):
        self.assertEqual([len(b) for b in up.batches(list(range(1201)))], [500, 500, 201])
        self.assertEqual(list(up.batches([])), [])

    def test_upsert_sends_each_batch_on_the_primary_key(self):
        client = MagicMock()
        up.upsert(client, "reviews", [{"data_id": "a", "review_id": str(i)} for i in range(1001)])
        calls = client.table.return_value.upsert.call_args_list
        self.assertEqual([len(c.args[0]) for c in calls], [500, 500, 1])
        self.assertTrue(all(c.kwargs["on_conflict"] == "data_id,review_id" for c in calls))

    def test_upsert_keys_are_the_migration_primary_keys(self):
        sql = next(up.MIGRATIONS_DIR.glob("*.sql")).read_text(encoding="utf-8")
        self.assertEqual(up.KEYS, {"restaurants": "data_id", "reviews": "data_id,review_id",
                                   "washroom_mentions": "mention_id", "washroom_summary": "data_id",
                                   "review_images": "storage_path"})
        self.assertIn("primary key (data_id, review_id)", sql)
        for col in ("data_id             text primary key", "mention_id       text primary key",
                    "storage_path    text primary key"):
            self.assertIn(col, sql)


class TestValidation(unittest.TestCase):

    def rows(self):
        return {"restaurants": [{"data_id": "a"}], "reviews": [{"data_id": "a", "review_id": "r"}],
                "washroom_mentions": [{"mention_id": "m", "data_id": "a", "review_id": "r"}],
                "washroom_summary": [{"data_id": "a"}],
                "review_images": [{"storage_path": "a/x.jpg", "data_id": "a", "review_id": None}]}

    def test_valid(self):
        self.assertEqual(up.validate(self.rows()), [])

    def test_duplicate_key_and_missing_parent(self):
        rows = self.rows()
        rows["reviews"].append({"data_id": "a", "review_id": "r"})
        rows["washroom_mentions"].append({"mention_id": "m2", "data_id": "a", "review_id": "gone"})
        problems = up.validate(rows)
        self.assertTrue(any("reviews: 1 duplicate" in p for p in problems))
        self.assertTrue(any("washroom_mentions: 1 rows point at a missing parent" in p for p in problems))


class TestImages(unittest.TestCase):

    LONG_ID = "Ci9DQUlRQUNvZENodHljRjlvT2tsNGFYaERiRGxOY20xbWJ6UklOV2xmUm01WlMyYxAB"

    def test_match_by_name(self):
        stem = up.eri.review_file_stem({"review_id": self.LONG_ID})          # truncated + sha1 hash
        self.assertEqual(up.match_by_name(f"{stem}_3.jpg", [self.LONG_ID, "other"]), (self.LONG_ID, 3))
        self.assertEqual(up.match_by_name("review_ChZabc_2.jpg", ["ChZabc"]), ("ChZabc", 2))
        self.assertEqual(up.match_by_name("restaurant_main.jpg", []), (None, 0))
        self.assertEqual(up.match_by_name("restaurant_photo_4.png", []), (None, 4))
        self.assertIsNone(up.match_by_name("review_unknown_1.jpg", ["ChZabc"]))

    def test_image_rows_skip_manifest_and_prefer_the_json_map(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(up.utils, "PROJECT_ROOT", Path(tmp)):
            images = Path(tmp) / "data" / "extracted_images"
            folder = images / "0x1_0x2"
            folder.mkdir(parents=True)
            (folder / "_manifest.json").write_text("{}")
            (folder / "restaurant_main.jpg").write_bytes(b"\xff\xd8main")
            (folder / "odd_name.jpg").write_bytes(b"\xff\xd8odd")
            known = {"data/extracted_images/0x1_0x2/odd_name.jpg": ("r1", 2)}
            items, problems = up.image_rows(images, ["0x1:0x2"], {"0x1:0x2": ["r1"]}, known)
        self.assertEqual(problems, [])
        rows = {r["storage_path"]: r for r, _ in items}
        self.assertEqual(set(rows), {"0x1_0x2/restaurant_main.jpg", "0x1_0x2/odd_name.jpg"})
        self.assertEqual((rows["0x1_0x2/odd_name.jpg"]["review_id"], rows["0x1_0x2/odd_name.jpg"]["image_index"]), ("r1", 2))
        main = rows["0x1_0x2/restaurant_main.jpg"]
        self.assertEqual((main["data_id"], main["review_id"], main["content_type"], main["file_size_bytes"]),
                         ("0x1:0x2", None, "image/jpeg", 6))
        self.assertEqual(len(main["sha256"]), 64)

    def test_upload_skips_files_with_the_same_sha256(self):
        client = MagicMock()
        client.table.return_value.select.return_value.execute.return_value.data = [
            {"storage_path": "f/same.jpg", "sha256": "aaa"}, {"storage_path": "f/changed.jpg", "sha256": "old"}]
        store = client.storage.from_.return_value
        store.list.return_value = [{"name": "same.jpg"}, {"name": "changed.jpg"}]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.jpg"
            p.write_bytes(b"x")
            items = [({"storage_path": f"f/{n}", "sha256": s, "content_type": "image/jpeg"}, p)
                     for n, s in (("same.jpg", "aaa"), ("changed.jpg", "new"), ("new.jpg", "bbb"))]
            self.assertEqual(up.upload_images(client, items), (2, 1))
        self.assertEqual([c.args[0] for c in store.upload.call_args_list], ["f/changed.jpg", "f/new.jpg"])


class TestCountCheck(unittest.TestCase):

    def test_compare_counts(self):
        self.assertEqual(up.compare_counts({"reviews": 5162}, {"reviews": 5162}), [])
        self.assertEqual(len(up.compare_counts({"reviews": 5162, "restaurants": 47}, {"reviews": 5161})), 2)

    def run_main(self, remote, argv=()):
        rows = {t: [{}] * n for t, n in {"restaurants": 2, "reviews": 3, "washroom_mentions": 1,
                                          "washroom_summary": 2, "review_images": 1}.items()}
        env = {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SECRET_KEY": "k",
               "SUPABASE_DB_URL": "postgresql://u:p@h:5432/postgres"}
        order = []
        with patch.object(up, "load_local", return_value=(rows, [], [])), \
                patch.object(up, "load_env", return_value=env), \
                patch.object(up, "apply_migrations"), patch.object(up, "wait_for_api"), \
                patch.object(up, "ensure_bucket"), patch.object(up, "upload_images", return_value=(0, 0)), \
                patch.object(up, "upsert", side_effect=lambda c, t, r: order.append(t)), \
                patch.object(up, "remote_counts", return_value=remote), \
                patch("supabase.create_client") as create:
            code = up.main(list(argv))
        return code, order, create

    def test_matching_counts_exit_zero_and_restaurants_go_first(self):
        code, order, _ = self.run_main({"restaurants": 2, "reviews": 3, "washroom_mentions": 1,
                                        "washroom_summary": 2, "review_images": 1})
        self.assertEqual(code, 0)
        self.assertEqual(order, up.TABLES)
        self.assertEqual(order[0], "restaurants")

    def test_mismatch_exits_non_zero(self):
        code, _, _ = self.run_main({"restaurants": 2, "reviews": 2, "washroom_mentions": 1,
                                    "washroom_summary": 2, "review_images": 1})
        self.assertEqual(code, 1)

    def test_only_and_skip_images(self):
        _, order, _ = self.run_main({"reviews": 3}, ["--only", "reviews"])
        self.assertEqual(order, ["reviews"])
        _, order, _ = self.run_main({}, ["--skip-images"])
        self.assertNotIn("review_images", order)

    def test_dry_run_writes_nothing(self):
        with patch.object(up, "load_local", return_value=({t: [] for t in up.TABLES}, [], [])), \
                patch.object(up, "load_env") as load_env, patch.object(up, "apply_migrations") as migrate:
            self.assertEqual(up.main(["--dry-run"]), 0)
        load_env.assert_not_called()
        migrate.assert_not_called()

    def test_validation_problem_exits_non_zero(self):
        with patch.object(up, "load_local", return_value=({t: [] for t in up.TABLES}, [], ["bad"])):
            self.assertEqual(up.main(["--dry-run"]), 1)


class TestSecrets(unittest.TestCase):

    def test_db_url_with_at_sign_in_password(self):
        kw = up.db_kwargs("postgresql://postgres.ref:p@ss:w0rd@x@aws-0-x.pooler.supabase.com:5432/postgres")
        self.assertEqual((kw["user"], kw["password"], kw["host"], kw["port"], kw["dbname"]),
                         ("postgres.ref", "p@ss:w0rd@x", "aws-0-x.pooler.supabase.com", 5432, "postgres"))

    def test_redact(self):
        out = up.redact("failed for key sb_secret_abc at postgresql://u:pw@h/db", ["sb_secret_abc"])
        self.assertNotIn("sb_secret_abc", out)
        self.assertNotIn("pw@h", out)

    def test_no_secret_key_in_site(self):
        site = _ROOT / "site"
        hits = [p.name for p in site.rglob("*") if p.is_file() and b"sb_secret_" in p.read_bytes()]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
