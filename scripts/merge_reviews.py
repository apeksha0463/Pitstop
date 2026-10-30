"""
scripts/merge_reviews.py
========================
Merge the reviews collected from every source into ONE dataset (0 credits).

Sources, read in this order:
  - data/output/washroom/*.json   SerpApi washroom searches  -> "serpapi_washroom_search"
  - data/output/*.json            SerpApi unfiltered          -> "serpapi"
  - data/output/apify/*.json      Apify unfiltered            -> "apify"
(data/output/archive/ is not read: superseded copies and places not in the list.)

Each review appears once per restaurant, keyed by its Google review_id (the
same in SerpApi and Apify). When several sources have it, the first one's
fields are kept and any field it lacks is filled from the others; ``sources``
lists every source that found it and ``matched_by`` every washroom search
that returned it. Dates are normalised to ``YYYY-MM-DDTHH:MM:SSZ``. Only the
restaurants of the restaurant list are written.

Writes:
  - data/processed/reviews/<data_id>_reviews.json   one file per restaurant
  - data/processed/all_reviews.csv                  one row per review (no reviewer names)

Usage (from the repository root):

    python scripts/merge_reviews.py
"""

from __future__ import annotations

import argparse
import csv
import datetime
import re
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
    from scripts.utils import load_json, make_review_fingerprint, safe_data_id, save_json, to_project_relative
except ImportError:
    import utils
    from utils import load_json, make_review_fingerprint, safe_data_id, save_json, to_project_relative

SOURCE_SERPAPI = "serpapi"
SOURCE_SERPAPI_SEARCH = "serpapi_washroom_search"
SOURCE_APIFY = "apify"
UNFILTERED_SOURCES = frozenset({SOURCE_SERPAPI, SOURCE_APIFY})
SOURCE_ORDER = (SOURCE_SERPAPI_SEARCH, SOURCE_SERPAPI, SOURCE_APIFY)

CSV_COLUMNS = [
    "restaurant", "data_id", "review_id", "iso_date", "rating", "sources", "matched_by",
    "language", "translated", "text", "review_link",
]


def merged_dir() -> Path:
    return utils.PROCESSED_DIR / "reviews"


def source_dirs() -> list[tuple[Path, str]]:
    """``[(folder, source)]`` in reading order."""
    return [(utils.filtered_dir(utils.OUTPUT_DIR, "washroom"), SOURCE_SERPAPI_SEARCH),
            (utils.OUTPUT_DIR, SOURCE_SERPAPI),
            (utils.OUTPUT_DIR / "apify", SOURCE_APIFY)]


def is_filtered_output(doc: dict) -> bool:
    """True for an output file written by a filtered (``--query`` / ``--topic-id``) run."""
    return bool(doc.get("review_filter") or doc.get("filter_runs"))


def normalise_date(value: Optional[str]) -> Optional[str]:
    """``2026-10-29T11:08:50.012Z`` / ``2026-10-29T11:08:50Z`` -> ``2026-10-29T11:08:50Z``."""
    if not value:
        return value
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", value)
    return f"{m.group(1)}Z" if m else value


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_into(base: dict, other: dict) -> None:
    for key, value in other.items():
        if key in ("sources", "matched_by"):
            continue
        if _empty(base.get(key)) and not _empty(value):
            base[key] = value
    base["matched_by"] = sorted(set(base.get("matched_by") or []) | set(other.get("matched_by") or []))


def load_sources(dirs: Optional[list[tuple[Path, str]]] = None) -> dict[str, list[dict]]:
    """
    Every collected review per data_id, merged across sources (see the module
    docstring). Each review carries ``sources`` (sorted list).
    """
    by_restaurant: dict[str, dict[str, dict]] = {}
    for folder, folder_source in dirs or source_dirs():
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                doc = load_json(path)
            except (OSError, ValueError):
                print(f"  [WARN] Skipping unreadable {to_project_relative(path)}")
                continue
            data_id = doc.get("data_id") if isinstance(doc, dict) else None
            if not data_id or not isinstance(doc.get("reviews"), list):
                continue
            source = SOURCE_SERPAPI_SEARCH if is_filtered_output(doc) else folder_source
            reviews = by_restaurant.setdefault(data_id, {})
            for raw in doc["reviews"]:
                if not isinstance(raw, dict):
                    continue
                review = {**raw, "iso_date": normalise_date(raw.get("iso_date"))}
                key = make_review_fingerprint(review)
                if key in reviews:
                    _merge_into(reviews[key], review)
                else:
                    reviews[key] = {**review, "matched_by": sorted(set(review.get("matched_by") or []))}
                    reviews[key]["sources"] = []
                if source not in reviews[key]["sources"]:
                    reviews[key]["sources"] = sorted(reviews[key]["sources"] + [source], key=SOURCE_ORDER.index)
    return {data_id: sorted(r.values(), key=lambda x: x.get("iso_date") or "", reverse=True)
            for data_id, r in by_restaurant.items()}


def write_merged(restaurants: list[dict], merged: dict[str, list[dict]]) -> dict:
    """Write the per-restaurant files and the CSV for *restaurants*. Returns totals."""
    folder = merged_dir()
    folder.mkdir(parents=True, exist_ok=True)
    listed = {r["data_id"] for r in restaurants}
    for stale in folder.glob("*_reviews.json"):
        if not any(stale.name == f"{safe_data_id(d)}_reviews.json" for d in listed):
            stale.unlink()   # a restaurant removed from the list
    rows = []
    saved_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in restaurants:
        reviews = merged.get(r["data_id"], [])
        by_source = {s: sum(s in x["sources"] for x in reviews) for s in SOURCE_ORDER}
        save_json({
            "restaurant":          r["restaurant"],
            "data_id":             r["data_id"],
            "google_review_count": r.get("google_review_count"),
            "reviews_total":       len(reviews),
            "reviews_with_text":   sum(1 for x in reviews if (x.get("text") or "").strip()),
            "reviews_by_source":   by_source,
            "saved_at":            saved_at,
            "reviews":             reviews,
        }, folder / f"{safe_data_id(r['data_id'])}_reviews.json")
        for x in reviews:
            rows.append({
                "restaurant":  r["restaurant"],
                "data_id":     r["data_id"],
                "review_id":   x.get("review_id") or "",
                "iso_date":    x.get("iso_date") or "",
                "rating":      x.get("rating") if x.get("rating") is not None else "",
                "sources":     ";".join(x["sources"]),
                "matched_by":  ";".join(x.get("matched_by") or []),
                "language":    x.get("language") or "",
                "translated":  bool(x.get("translated")),
                "text":        re.sub(r"\s+", " ", x.get("text") or "").strip(),
                "review_link": x.get("review_link") or "",
            })
    csv_path = utils.PROCESSED_DIR / "all_reviews.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return {"restaurants": len(restaurants), "reviews": len(rows),
            "ignored_places": sorted(set(merged) - listed), "csv": csv_path}


def build(restaurants: list[dict]) -> dict[str, list[dict]]:
    """Merge every source, write the merged dataset and return it (only listed restaurants)."""
    merged = load_sources()
    totals = write_merged(restaurants, merged)
    if totals["ignored_places"]:
        print(f"  [INFO] {len(totals['ignored_places'])} place(s) not in the restaurant list were left out.")
    return {r["data_id"]: merged.get(r["data_id"], []) for r in restaurants}


def main(argv: Optional[list[str]] = None) -> int:
    try:
        from scripts.extract_washroom_mentions import DEFAULT_CSV, load_restaurants
    except ImportError:
        from extract_washroom_mentions import DEFAULT_CSV, load_restaurants
    parser = argparse.ArgumentParser(description="Merge the reviews of every source into one dataset.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    args = parser.parse_args(argv)
    restaurants = load_restaurants(args.csv)
    merged = build(restaurants)
    reviews = [x for rs in merged.values() for x in rs]
    print(f"Restaurants       : {len(restaurants)}")
    print(f"Unique reviews    : {len(reviews)}")
    for source in SOURCE_ORDER:
        print(f"  from {source:24}: {sum(source in x['sources'] for x in reviews)}")
    print(f"  found by 2+ sources        : {sum(len(x['sources']) > 1 for x in reviews)}")
    print(f"Per restaurant    : {to_project_relative(merged_dir())}")
    print(f"CSV               : {to_project_relative(utils.PROCESSED_DIR / 'all_reviews.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
