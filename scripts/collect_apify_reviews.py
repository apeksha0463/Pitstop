"""
scripts/collect_apify_reviews.py
================================
Download the newest reviews (no filter) of every restaurant in
data/raw_data/restaurants_route_above_3.5.csv with Apify's "Google Maps
Reviews Scraper" (compass/Google-Maps-Reviews-Scraper), the alternative to
SerpApi when its pages come back truncated.

Each restaurant is addressed by its Google CID (the second part of its
data_id), as ``https://www.google.com/maps?cid=<cid>``. One actor run covers
all requested restaurants; its spend is capped with Apify's
``maxTotalChargeUsd`` (the actor charges per review scraped). Reviewer
personal data is not requested (``personalData=false``); review IDs are the
same as SerpApi's, so reviews found by both are counted once.

Restaurants that already have --max-reviews Apify reviews (or every review
Google offers) are skipped unless --refresh is passed.

Reads the token from APIFY_TOKEN in sources/.env (never printed).

Writes:
  - data/raw_data/apify/reviews_<timestamp>.json   the run's dataset, as returned
  - data/output/apify/<data_id>_reviews.json       reviews per restaurant (merged)

Usage (from the repository root):

    python scripts/collect_apify_reviews.py --max-charge-usd 3
    python scripts/collect_apify_reviews.py --limit 1 --max-charge-usd 0.1   # test one restaurant
    python scripts/collect_apify_reviews.py --from-dataset data/raw_data/apify/reviews_X.json  # 0 cost
"""

from __future__ import annotations

import argparse
import datetime
import html
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import dotenv_values

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
    from scripts.extract_washroom_mentions import DEFAULT_CSV, load_restaurants
    from scripts.utils import (
        DEFAULT_ENV_FILE, DIVIDER, data_cid_from_data_id, load_json, name_matches, safe_data_id, save_json,
        to_project_relative,
    )
except ImportError:
    import utils
    from extract_washroom_mentions import DEFAULT_CSV, load_restaurants
    from utils import (
        DEFAULT_ENV_FILE, DIVIDER, data_cid_from_data_id, load_json, name_matches, safe_data_id, save_json,
        to_project_relative,
    )

ACTOR = "compass~Google-Maps-Reviews-Scraper"
API = "https://api.apify.com/v2"
SOURCE = "Google Maps via Apify (compass/Google-Maps-Reviews-Scraper)"
DEFAULT_MAX_REVIEWS: int = 100
DEFAULT_MAX_CHARGE_USD: float = 1.0
POLL_SECONDS: float = 10
RUN_TIMEOUT_SECONDS: int = 3600
FINISHED = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
# Google's count also includes some reviews no scraper gets: within 10% counts as all of them
COMPLETE_SHARE = 0.9


def apify_dir() -> Path:
    return utils.OUTPUT_DIR / "apify"


def raw_dir() -> Path:
    return utils.RAW_DATA_DIR / "apify"


def output_path(data_id: str) -> Path:
    return apify_dir() / f"{safe_data_id(data_id)}_reviews.json"


def load_token(env_path: Optional[Path] = None) -> str:
    """APIFY_TOKEN from sources/.env (or the environment). Exits when missing; never printed."""
    path = Path(env_path) if env_path is not None else DEFAULT_ENV_FILE
    token = ""
    if path.exists():
        token = (dotenv_values(path, encoding="utf-8-sig").get("APIFY_TOKEN") or "").strip()
    token = token or os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        print(f"[ERROR] APIFY_TOKEN not found. Add a line APIFY_TOKEN=... to {to_project_relative(path)}")
        sys.exit(1)
    return token


def place_url(data_id: str) -> str:
    return f"https://www.google.com/maps?cid={data_cid_from_data_id(data_id)}"


# ---------------------------------------------------------------------------
# Converting Apify items
# ---------------------------------------------------------------------------

def _text(value) -> Optional[str]:
    """Plain review text: Apify writes line breaks as ``<br>`` and may keep HTML entities."""
    if not isinstance(value, str):
        return None
    value = html.unescape(re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)).strip()
    return value or None


def flatten_item(item: dict) -> dict:
    """One Apify review item in the shape of utils.flatten_review (SerpApi reviews)."""
    original = _text(item.get("text"))
    translated = _text(item.get("textTranslated"))
    stars = item.get("stars")
    try:
        rating = float(stars) if stars not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    owner = _text(item.get("responseFromOwnerText"))
    images = [{"image_url": url, "local_path": None, "download_status": "not_requested"}
              for url in item.get("reviewImageUrls") or [] if isinstance(url, str)]
    return {
        "review_id":              item.get("reviewId") or None,
        "author":                 item.get("name") or None,
        "author_url":             item.get("reviewerUrl") or None,
        "author_local_guide":     bool(item.get("isLocalGuide")),
        "author_reviews_count":   item.get("reviewerNumberOfReviews") or None,
        "author_photos_count":    None,
        "rating":                 rating,
        "date":                   item.get("publishAt") or None,
        "iso_date":               item.get("publishedAtDate") or None,
        "iso_date_of_last_edit":  None,
        # English first so the washroom matching sees it; the original is kept below
        "text":                   translated or original,
        "text_original_language": original if translated else None,
        "language":               item.get("originalLanguage") or item.get("language") or None,
        "translated":             bool(translated),
        "likes":                  item.get("likesCount") or 0,
        "details":                item.get("reviewDetailedRating") or None,
        "owner_response":         {"text": owner, "date": item.get("responseFromOwnerDate")} if owner else None,
        "review_link":            item.get("reviewUrl") or None,
        "images_count":           len(images),
        "images":                 images,
        "source":                 SOURCE,
    }


def group_by_restaurant(items: list[dict], restaurants: list[dict]) -> dict[str, list[dict]]:
    """Dataset items per data_id (matched on the CID). Items of other places are dropped."""
    by_cid = {data_cid_from_data_id(r["data_id"]): r["data_id"] for r in restaurants}
    grouped: dict[str, list[dict]] = {}
    for item in items:
        data_id = by_cid.get(str(item.get("cid") or ""))
        if data_id and item.get("reviewId"):
            grouped.setdefault(data_id, []).append(item)
    return grouped


def save_restaurant(r: dict, items: list[dict], *, run_id: Optional[str], requested: int) -> dict:
    """Merge *items* into the restaurant's Apify file (dedupe by review_id, newest first) and save it."""
    path = output_path(r["data_id"])
    previous = []
    if path.exists():
        try:
            previous = load_json(path).get("reviews") or []
        except (OSError, ValueError, AttributeError):
            print(f"  [WARN] {path.name} is unreadable; it will be replaced.")
    reviews: dict[str, dict] = {}
    for review in [flatten_item(i) for i in items] + previous:
        reviews.setdefault(review["review_id"], review)
    ordered = sorted(reviews.values(), key=lambda x: x.get("iso_date") or "", reverse=True)
    place = items[0] if items else {}
    title = place.get("title")
    doc = {
        "restaurant_name":                  title or "Unknown",
        "data_id":                          r["data_id"],
        "expected_restaurant_name":         r["restaurant"],
        "restaurant_name_matches_expected": name_matches(title, r["restaurant"]) if title else None,
        "google_place_id":                  place.get("placeId"),
        "address":                          place.get("address"),
        "rating":                           place.get("totalScore"),
        "total_reviews_reported":           place.get("reviewsCount"),
        "source":                           SOURCE,
        "reviews_requested":                requested,
        "reviews_returned_last_run":        len(items),
        "reviews_collected":                len(ordered),
        "last_run_id":                      run_id,
        "saved_at":                         datetime.datetime.now().isoformat(timespec="seconds"),
        "reviews":                          ordered,
    }
    save_json(doc, path)
    return doc


def is_done(r: dict, target: int) -> bool:
    """True when the restaurant already has *target* Apify reviews, or (nearly) all Google has."""
    path = output_path(r["data_id"])
    if not path.exists():
        return False
    try:
        doc = load_json(path)
    except (OSError, ValueError):
        return False
    have = doc.get("reviews_collected") or 0
    total = doc.get("total_reviews_reported")
    return have >= target or (isinstance(total, int) and total < target and have >= COMPLETE_SHARE * total)


# ---------------------------------------------------------------------------
# Apify API
# ---------------------------------------------------------------------------

def _request(method: str, path: str, token: str, **kwargs) -> dict:
    resp = requests.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                            timeout=60, **kwargs)
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error") or {}
        except ValueError:
            err = {}
        raise RuntimeError(f"Apify {method} {path.split('?')[0]} failed: HTTP {resp.status_code} "
                           f"{err.get('type', '')} {err.get('message', '')}".strip())
    return resp.json()


def monthly_usage(token: str) -> tuple[Optional[float], Optional[float]]:
    """``(used_usd, limit_usd)`` this month (free account check)."""
    data = _request("GET", "/users/me/limits", token).get("data") or {}
    return (data.get("current") or {}).get("monthlyUsageUsd"), (data.get("limits") or {}).get("maxMonthlyUsageUsd")


def run_actor(token: str, restaurants: list[dict], max_reviews: int, max_charge_usd: float,
              poll: float = POLL_SECONDS) -> tuple[dict, list[dict]]:
    """Start one actor run for *restaurants*, wait for it and return ``(run, dataset items)``."""
    run_input = {
        "startUrls":     [{"url": place_url(r["data_id"])} for r in restaurants],
        "maxReviews":    max_reviews,
        "reviewsSort":   "newest",
        "language":      "en",
        "reviewsOrigin": "google",
        "personalData":  False,
    }
    run = _request("POST", f"/acts/{ACTOR}/runs?maxTotalChargeUsd={max_charge_usd}", token,
                   json=run_input)["data"]
    print(f"Apify run {run['id']} started for {len(restaurants)} restaurant(s), "
          f"spend capped at ${max_charge_usd:.2f}.")
    started = time.monotonic()
    while run["status"] not in FINISHED:
        if time.monotonic() - started > RUN_TIMEOUT_SECONDS:
            raise RuntimeError(f"Apify run {run['id']} still {run['status']} after {RUN_TIMEOUT_SECONDS}s; "
                               "import it later with --run-id.")
        time.sleep(poll)
        run = _request("GET", f"/actor-runs/{run['id']}", token)["data"]
        print(f"  ... {run['status']}")
    return run, fetch_items(token, run)


def fetch_items(token: str, run: dict) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        page = _request("GET", f"/datasets/{run['defaultDatasetId']}/items?format=json&clean=true"
                               f"&offset={offset}&limit=1000", token)
        if not isinstance(page, list) or not page:
            return items
        items.extend(page)
        offset += len(page)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def import_items(restaurants: list[dict], items: list[dict], *, run_id: Optional[str], requested: int) -> list[dict]:
    """Save the items per restaurant and return one summary row per restaurant in *restaurants*."""
    grouped = group_by_restaurant(items, restaurants)
    rows = []
    for r in restaurants:
        doc = save_restaurant(r, grouped.get(r["data_id"], []), run_id=run_id, requested=requested)
        rows.append({"restaurant": r["restaurant"], "data_id": r["data_id"],
                     "returned": doc["reviews_returned_last_run"], "collected": doc["reviews_collected"],
                     "google_total": doc["total_reviews_reported"],
                     "name_ok": doc["restaurant_name_matches_expected"], "api_name": doc["restaurant_name"]})
    return rows


def print_rows(rows: list[dict], target: int) -> None:
    print(f"\n{DIVIDER}")
    for row in rows:
        flag = ""
        if row["name_ok"] is False:
            flag = f"  [CHECK: Google calls it '{row['api_name']}']"
        elif not row["returned"]:
            flag = "  [NO REVIEWS RETURNED]"
        print(f"  {row['restaurant'][:38]:38} {row['returned']:>4} new, {row['collected']:>4} total"
              f" (Google: {row['google_total']}){flag}")
    short = [r for r in rows if r["collected"] < target and not
             (isinstance(r["google_total"], int) and r["collected"] >= COMPLETE_SHARE * min(target, r["google_total"]))]
    print(DIVIDER)
    print(f"  Restaurants with the target ({target}) or all their reviews: {len(rows) - len(short)} / {len(rows)}")
    print(f"  Saved under {to_project_relative(apify_dir())}")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the newest reviews of every listed restaurant via Apify.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    parser.add_argument("--max-reviews", type=int, default=DEFAULT_MAX_REVIEWS,
                        help=f"Newest reviews per restaurant (default {DEFAULT_MAX_REVIEWS}).")
    parser.add_argument("--max-charge-usd", type=float, default=DEFAULT_MAX_CHARGE_USD,
                        help=f"Spend cap for the Apify run in USD (default {DEFAULT_MAX_CHARGE_USD}).")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="Only the first N restaurants.")
    parser.add_argument("--refresh", action="store_true", help="Also fetch restaurants that already have enough.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--from-dataset", type=Path, metavar="FILE", help="Import a saved dataset file (no cost).")
    source.add_argument("--run-id", metavar="ID", help="Import the dataset of an earlier Apify run (no cost).")
    args = parser.parse_args(argv)
    if args.max_reviews < 1:
        parser.error("--max-reviews must be at least 1")
    if args.max_charge_usd <= 0:
        parser.error("--max-charge-usd must be positive")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    restaurants = load_restaurants(args.csv)[: args.limit]
    print(f"\n{DIVIDER}\n   APIFY REVIEW COLLECTOR\n{DIVIDER}\n")
    print(f"Restaurants: {len(restaurants)} from {to_project_relative(args.csv)}")

    if args.from_dataset:
        items = load_json(args.from_dataset)
        grouped = group_by_restaurant(items, restaurants)
        rows = import_items([r for r in restaurants if r["data_id"] in grouped], items,
                            run_id=None, requested=args.max_reviews)
        print_rows(rows, args.max_reviews)
        return 0

    token = load_token()
    if args.run_id:
        run = _request("GET", f"/actor-runs/{args.run_id}", token)["data"]
        items = fetch_items(token, run)
    else:
        todo = restaurants if args.refresh else [r for r in restaurants if not is_done(r, args.max_reviews)]
        if len(todo) < len(restaurants):
            print(f"Skipping {len(restaurants) - len(todo)} restaurant(s) that already have enough "
                  "(use --refresh to fetch them again).")
        if not todo:
            return 0
        used, limit = monthly_usage(token)
        print(f"Apify usage this month: ${used or 0:.2f} of ${limit if limit is not None else '?'}")
        if used is not None and limit is not None and used + args.max_charge_usd > limit:
            print(f"[ERROR] A ${args.max_charge_usd:.2f} cap would exceed this month's ${limit} limit.")
            return 1
        try:
            run, items = run_actor(token, todo, args.max_reviews, args.max_charge_usd)
        except (RuntimeError, requests.RequestException) as exc:
            print(f"[ERROR] {exc}")
            return 1
        restaurants = todo
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dataset_path = raw_dir() / f"reviews_{stamp}.json"
    save_json(items, dataset_path)
    print(f"Run {run['id']}: {run['status']}, {len(items)} item(s), cost ${run.get('usageTotalUsd') or 0:.2f}. "
          f"Dataset saved to {to_project_relative(dataset_path)}")
    rows = import_items(restaurants, items, run_id=run["id"], requested=args.max_reviews)
    print_rows(rows, args.max_reviews)
    return 0 if run["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
