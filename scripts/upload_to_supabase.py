"""
scripts/upload_to_supabase.py
=============================
Loads every Pitstop dataset into Supabase and checks the result.

  1. Applies supabase/migrations/*.sql with psycopg (SUPABASE_DB_URL). The SQL is
     idempotent; nothing is ever dropped or truncated.
  2. Upserts, in batches of 500 and in foreign-key order (restaurants first), on
     each table's primary key, so re-runs never duplicate:
       restaurants        seed CSV + route_km/side/detour_km from build_route_map.py
       reviews            data/processed/all_reviews.csv (no reviewer names or links)
       washroom_mentions  data/processed/washroom_mentions_scored.csv
       washroom_summary   washroom_summary.csv + washroom_sentiment_summary.csv + grade
       review_images      data/extracted_images/<folder>/<file>, mapped to reviews via the
                          per-restaurant JSON in data/output/ (filename rule as fallback)
  3. Uploads the image files to the public Storage bucket "review-images" as
     <folder>/<file>, skipping files already there with the same sha256.
  4. Counts the rows in Supabase and compares them with the local files.
     Exits non-zero on any mismatch.

Keys come only from sources/.env and are never printed: SUPABASE_URL and
SUPABASE_SECRET_KEY (writes bypass RLS), SUPABASE_DB_URL (migration).

Usage (from the repository root):

    python scripts/upload_to_supabase.py --dry-run         # validate and count, no writes
    python scripts/upload_to_supabase.py                   # everything
    python scripts/upload_to_supabase.py --only reviews    # one table
    python scripts/upload_to_supabase.py --skip-images     # tables only, no Storage
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import mimetypes
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import build_route_map as brm, extract_review_images as eri, utils
except ImportError:
    import build_route_map as brm
    import extract_review_images as eri
    import utils

MIGRATIONS_DIR = utils.PROJECT_ROOT / "supabase" / "migrations"
REVIEWS_CSV = utils.PROCESSED_DIR / "all_reviews.csv"
SUMMARY_CSV = utils.PROCESSED_DIR / "washroom_summary.csv"
BUCKET = "review-images"
BATCH_SIZE = 500
TABLES = ["restaurants", "reviews", "washroom_mentions", "washroom_summary", "review_images"]   # FK order
KEYS = {"restaurants": "data_id", "reviews": "data_id,review_id", "washroom_mentions": "mention_id",
        "washroom_summary": "data_id", "review_images": "storage_path"}


# ---------------------------------------------------------------------------
# File -> row conversion
# ---------------------------------------------------------------------------

def _text(v):
    return v if v not in ("", None) else None


def _num(v):
    return float(v) if v not in ("", None) else None


def _int(v):
    return int(float(v)) if v not in ("", None) else None


def _bool(v) -> bool:
    return str(v).strip().lower() in ("true", "1")


def _list(v) -> list[str]:
    return [p for p in (v or "").split(";") if p]


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def restaurant_rows(stations: list[dict], seed: list[dict]) -> list[dict]:
    """One row per station from build_route_map.load_stations, plus the seed CSV's Location.

    route_km, side and detour_km are rounded exactly as build_route_map.site_data does.
    """
    location = {r["data_id"]: r.get("Location") for r in seed}
    return [{
        "data_id": s["data_id"], "name": s["name"], "location": _text(location.get(s["data_id"])),
        "lat": s["lat"], "lng": s["lng"], "google_rating": _num(s["rating"]),
        "google_review_count": s["google_reviews"], "cuisine": _text(s["cuisine"]), "position": _text(s["position"]),
        "route_km": round(s["along_m"] / 1000, 2),
        "side": "west" if s["side"] == "left" else "east",
        "detour_km": round(s["dist_m"] / 1000, 2) if s["detour"] else None,
        "maps_link": _text(s["maps_url"]), "position_note": _text(s["position_note"]),
    } for s in stations]


def review_row(r: dict) -> dict:
    return {"data_id": r["data_id"], "review_id": r["review_id"], "iso_date": _text(r["iso_date"]),
            "rating": _num(r["rating"]), "text": _text(r["text"]), "language": _text(r["language"]),
            "translated": _bool(r["translated"]), "sources": _list(r["sources"]),
            "matched_by": _list(r["matched_by"]), "review_link": _text(r["review_link"])}


MENTION_FLOATS = ("star_rating", "sentiment_score", "confidence", "p_negative", "p_neutral", "p_positive")


def mention_row(r: dict) -> dict:
    """Every column of washroom_mentions_scored.csv, typed."""
    row = {k: _text(v) for k, v in r.items()}
    row.update({k: _num(r[k]) for k in MENTION_FLOATS})
    row["passage_count"] = _int(r["passage_count"])
    row["needs_review"] = _bool(r["needs_review"])
    return row


SUMMARY_INTS = ("google_review_count", "sample_reviews", "sample_reviews_with_text", "washroom_in_sample",
                "washroom_search_reviews", "washroom_related_reviews", "recent_reviews", "needs_review")
SUMMARY_COUNTS = ("washroom_reviews", "positive", "neutral", "negative")          # not null, default 0
SUMMARY_FLOATS = ("washroom_share", "mean_sentiment_score", "share_negative", "recent_mean_sentiment_score")


def summary_rows(summary: list[dict], sentiment: list[dict]) -> list[dict]:
    """washroom_summary.csv joined with washroom_sentiment_summary.csv on data_id, plus the grade."""
    by_id = {r["data_id"]: r for r in sentiment}
    rows = []
    for s in summary:
        r = {**s, **by_id.get(s["data_id"], {})}
        row = {"data_id": r["data_id"], "latest_mention_date": _text(r.get("latest_mention_date")),
               "low_data": _bool(r.get("low_data", "True"))}
        row.update({k: _int(r.get(k)) for k in SUMMARY_INTS})
        row.update({k: _int(r.get(k)) or 0 for k in SUMMARY_COUNTS})
        row.update({k: _num(r.get(k)) for k in SUMMARY_FLOATS})
        row["grade"] = brm.washroom_bucket(row["washroom_reviews"], row["mean_sentiment_score"])
        rows.append(row)
    return rows


def json_image_map(json_dir: Path) -> dict[str, tuple[str | None, int]]:
    """local_path -> (review_id, image_index) from the per-restaurant JSON in data/output/."""
    known = {}
    for f in sorted(json_dir.glob("*.json")):
        doc = utils.load_json(f)
        main = doc.get("main_restaurant_photo") or {}
        if main.get("local_path"):
            known[main["local_path"]] = (None, 0)
        for r in doc.get("reviews") or []:
            for im in r.get("images") or []:
                if isinstance(im, dict) and im.get("local_path") and im.get("image_index") is not None:
                    known[im["local_path"]] = (r.get("review_id"), int(im["image_index"]))
    return known


def match_by_name(name: str, review_ids) -> tuple[str | None, int] | None:
    """(review_id, image_index) from the file name alone, using the downloader's naming rule."""
    stem = Path(name).stem
    if re.fullmatch(r"restaurant_main(_\d+)?", stem):
        return None, 0
    if m := re.fullmatch(r"restaurant_photo_(\d+)", stem):
        return None, int(m[1])
    base, _, n = stem.rpartition("_")
    if not n.isdigit():
        return None
    stems = {eri.review_file_stem({"review_id": rid}): rid for rid in review_ids}
    rid = stems.get(base) or stems.get(base.removeprefix("review_"))
    return (rid, int(n)) if rid else None


def image_rows(images_dir: Path, restaurant_ids, review_ids_by_restaurant: dict, known: dict):
    """([(row, file path)], [problems]) for every image file except _manifest.json."""
    folders = {utils.safe_data_id(d): d for d in restaurant_ids}
    items, problems = [], []
    for path in sorted(p for p in images_dir.glob("*/*") if p.is_file() and p.name != "_manifest.json"):
        data_id = folders.get(path.parent.name)
        if data_id is None:
            problems.append(f"image folder of no listed restaurant: {path.parent.name}")
            continue
        rel = f"{utils.to_project_relative(images_dir)}/{path.parent.name}/{path.name}"
        hit = known.get(rel) or match_by_name(path.name, review_ids_by_restaurant.get(data_id, ()))
        if hit is None:
            problems.append(f"image matches no review: {rel}")
            continue
        data = path.read_bytes()
        items.append(({"storage_path": f"{path.parent.name}/{path.name}", "data_id": data_id,
                       "review_id": hit[0], "image_index": hit[1], "sha256": hashlib.sha256(data).hexdigest(),
                       "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                       "file_size_bytes": len(data)}, path))
    return items, problems


def load_local(offline_route: bool = False) -> tuple[dict[str, list[dict]], list[tuple[dict, Path]], list[str]]:
    """All rows by table, the image files, and any validation problems."""
    latlngs, source = brm.fetch_route(offline_route)
    print(f"Route for route_km/side/detour_km: {source}")
    stations = brm.load_stations(brm.build_route(latlngs))
    rows = {
        "restaurants": restaurant_rows(stations, read_csv(brm.RESTAURANTS_CSV)),
        "reviews": [review_row(r) for r in read_csv(REVIEWS_CSV)],
        "washroom_mentions": [mention_row(r) for r in read_csv(brm.MENTIONS_CSV)],
        "washroom_summary": summary_rows(read_csv(SUMMARY_CSV), read_csv(brm.SUMMARY_CSV)),
    }
    ids_by_restaurant: dict[str, list[str]] = {}
    for r in rows["reviews"]:
        ids_by_restaurant.setdefault(r["data_id"], []).append(r["review_id"])
    images, problems = image_rows(utils.EXTRACTED_IMAGES_DIR, [r["data_id"] for r in rows["restaurants"]],
                                  ids_by_restaurant, json_image_map(utils.OUTPUT_DIR))
    rows["review_images"] = [row for row, _ in images]
    return rows, images, problems + validate(rows)


def validate(rows: dict[str, list[dict]]) -> list[str]:
    """Primary keys unique and every foreign key present, checked locally before any write."""
    problems = []
    for table, key in KEYS.items():
        cols = key.split(",")
        keys = [tuple(r[c] for c in cols) for r in rows.get(table, [])]
        if len(set(keys)) != len(keys):
            problems.append(f"{table}: {len(keys) - len(set(keys))} duplicate primary keys")
    restaurants = {r["data_id"] for r in rows["restaurants"]}
    reviews = {(r["data_id"], r["review_id"]) for r in rows["reviews"]}
    checks = [("reviews", lambda r: r["data_id"] in restaurants),
              ("washroom_summary", lambda r: r["data_id"] in restaurants),
              ("washroom_mentions", lambda r: (r["data_id"], r["review_id"]) in reviews),
              ("review_images", lambda r: r["data_id"] in restaurants
               and (r["review_id"] is None or (r["data_id"], r["review_id"]) in reviews))]
    for table, ok in checks:
        bad = sum(not ok(r) for r in rows.get(table, []))
        if bad:
            problems.append(f"{table}: {bad} rows point at a missing parent row")
    return problems


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def load_env(*names: str) -> dict[str, str]:
    """The named keys from sources/.env only (never the process environment)."""
    from dotenv import dotenv_values
    env = dotenv_values(utils.DEFAULT_ENV_FILE)
    missing = [n for n in names if not (env.get(n) or "").strip()]
    if missing:
        raise SystemExit(f"[ERROR] Missing in {utils.to_project_relative(utils.DEFAULT_ENV_FILE)}: {', '.join(missing)}")
    return {n: env[n].strip() for n in names}


def db_kwargs(url: str) -> dict:
    """psycopg connect arguments from a Postgres URL whose password may hold unescaped '@' or ':'."""
    scheme, rest = url.split("://", 1)
    creds, hostpart = rest.rsplit("@", 1)
    user, _, password = creds.partition(":")
    u = urlsplit(f"{scheme}://{hostpart}")
    return {"host": u.hostname, "port": u.port or 5432, "dbname": u.path.lstrip("/") or "postgres",
            "user": unquote(user), "password": unquote(password), "sslmode": "require"}


def apply_migrations(db_url: str) -> None:
    import psycopg
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with psycopg.connect(connect_timeout=20, autocommit=True, **db_kwargs(db_url)) as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"Applied {utils.to_project_relative(f)}")
        conn.execute("notify pgrst, 'reload schema'")


def wait_for_api(client, tries: int = 15) -> None:
    """PostgREST reloads its schema cache asynchronously after DDL."""
    for i in range(tries):
        try:
            client.table("restaurants").select("data_id", head=True).execute()
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1)


def batches(rows: list, size: int = BATCH_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def upsert(client, table: str, rows: list[dict]) -> None:
    from postgrest import ReturnMethod
    for chunk in batches(rows):
        client.table(table).upsert(chunk, on_conflict=KEYS[table], returning=ReturnMethod.minimal).execute()
    print(f"  {table:18} upserted {len(rows):>5} rows")


def ensure_bucket(client) -> None:
    from storage3.utils import StorageException
    try:
        bucket = client.storage.get_bucket(BUCKET)
    except StorageException:
        client.storage.create_bucket(BUCKET, options={"public": True})
        print(f"  Created public bucket {BUCKET}")
        return
    if not bucket.public:
        client.storage.update_bucket(BUCKET, {"public": True})


def upload_images(client, images: list[tuple[dict, Path]]) -> tuple[int, int]:
    """Upload files whose sha256 is not already recorded for an object that exists. (uploaded, skipped)"""
    # ponytail: one read of review_images, capped by PostgREST max-rows (1000); page it past that
    recorded = {r["storage_path"]: r["sha256"]
                for r in client.table("review_images").select("storage_path,sha256").execute().data}
    store = client.storage.from_(BUCKET)
    present: dict[str, set[str]] = {}
    uploaded = skipped = 0
    for row, path in images:
        folder, name = row["storage_path"].split("/", 1)
        if folder not in present:
            present[folder] = {o["name"] for o in store.list(folder, {"limit": 1000})}
        if recorded.get(row["storage_path"]) == row["sha256"] and name in present[folder]:
            skipped += 1
            continue
        store.upload(row["storage_path"], path.read_bytes(),
                     {"content-type": row["content_type"], "upsert": "true", "cache-control": "31536000"})
        uploaded += 1
    return uploaded, skipped


def remote_counts(client, tables) -> dict[str, int]:
    return {t: client.table(t).select("*", count="exact", head=True).execute().count for t in tables}


def compare_counts(local: dict[str, int], remote: dict[str, int]) -> list[str]:
    """Print both counts per table; return the mismatches."""
    print(f"\n  {'table':18} {'local':>6} {'Supabase':>9}")
    bad = []
    for t, n in local.items():
        r = remote.get(t)
        print(f"  {t:18} {n:>6} {'—' if r is None else r:>9}  {'OK' if r == n else 'MISMATCH'}")
        if r != n:
            bad.append(f"{t}: {n} local rows, {r} in Supabase")
    return bad


def redact(text: str, secrets) -> str:
    for s in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
    return re.sub(r"postgres(ql)?://\S+", "[REDACTED]", text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="validate and count, no writes")
    parser.add_argument("--only", choices=TABLES, help="upload this table only")
    parser.add_argument("--skip-images", action="store_true", help="skip Storage and review_images")
    args = parser.parse_args(argv)
    if args.only == "review_images" and args.skip_images:
        parser.error("--only review_images and --skip-images contradict each other")
    tables = [t for t in TABLES if (args.only in (None, t)) and not (args.skip_images and t == "review_images")]

    rows, images, problems = load_local()
    local = {t: len(rows[t]) for t in tables}
    print("Local rows: " + ", ".join(f"{t} {n}" for t, n in local.items()))
    if problems:
        print("\n[ERROR] Validation failed:\n  " + "\n  ".join(problems))
        return 1
    if args.dry_run:
        print("Dry run: data valid, nothing written.")
        return 0

    env = load_env("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_DB_URL")
    secrets = [env["SUPABASE_SECRET_KEY"], env["SUPABASE_DB_URL"], db_kwargs(env["SUPABASE_DB_URL"])["password"]]
    try:
        from supabase import create_client
        apply_migrations(env["SUPABASE_DB_URL"])
        client = create_client(env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"])
        wait_for_api(client)
        for t in tables:
            if t == "review_images":
                ensure_bucket(client)
                uploaded, skipped = upload_images(client, images)
                print(f"  Storage {BUCKET}: uploaded {uploaded}, unchanged {skipped}")
            upsert(client, t, rows[t])
        mismatches = compare_counts(local, remote_counts(client, tables))
    except Exception as exc:          # every message is redacted: no key or DB URL ever reaches the console
        print(f"\n[ERROR] {type(exc).__name__}: {redact(str(exc), secrets)}")
        return 1
    if mismatches:
        print("\n[ERROR] Count mismatch:\n  " + "\n  ".join(mismatches))
        return 1
    print("\nAll counts match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
