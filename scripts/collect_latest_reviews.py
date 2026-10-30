"""
scripts/collect_latest_reviews.py
=================================
Download the newest reviews (no filter) of every restaurant in
data/raw_data/restaurants_route_above_3.5.csv, up to --max-reviews each,
within a SerpApi credit budget, so the washroom matching runs on a known,
complete sample instead of Google's partial in-review search.

Pages are fetched breadth-first: page 1 of every restaurant, then page 2,
and so on while budget remains. Page 1 holds about 10 reviews, later pages
20; each page costs 1 credit and is saved straight away. Where every
restaurant stopped is kept in data/output/latest_reviews/progress.json, so
the next run (next month's credits, or a larger --max-reviews) continues
from there. Pages saved by earlier runs are reused instead of fetched again.

Per restaurant the coverage is one of:
  - complete     every review Google offers was downloaded (fewer than the target exist)
  - target_met   at least --max-reviews of the newest reviews downloaded
  - in_progress  some pages downloaded, more needed for the target
  - not_started  nothing downloaded yet
  - ended_early  SerpApi stopped offering pages well short of the target and of
                 Google's count (typically a page of 5 reviews with no next
                 page). Retrying right away does not help, so it is only
                 fetched again from page 1 with --retry-ended-early.

When several pages in a row end early, SerpApi is serving truncated results
for every place, so the run stops instead of spending more credits.

Writes:
  - data/output/<data_id>_reviews.json             reviews (merged per restaurant)
  - data/output/latest_reviews/progress.json       where to continue per restaurant
  - data/output/latest_reviews/coverage.json       coverage per restaurant

Usage (from the repository root):

    python scripts/collect_latest_reviews.py --max-credits 40
    python scripts/collect_latest_reviews.py --max-reviews 200 --max-credits 200
    python scripts/collect_latest_reviews.py --report     # coverage only, 0 credits
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import math
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import extract_reviews, utils
    from scripts.collect_washroom_reviews import check_credits
    from scripts.extract_washroom_mentions import (
        DEFAULT_CSV, SOURCE_UNFILTERED, collected_reviews, load_restaurants,
    )
    from scripts.utils import (
        DIVIDER, REQUEST_DELAY_SECONDS, REVIEWS_PER_PAGE, ensure_dirs, load_api_key, load_json,
        make_cache_manifest_path, make_raw_page_path, save_json, to_project_relative,
    )
except ImportError:
    import extract_reviews
    import utils
    from collect_washroom_reviews import check_credits
    from extract_washroom_mentions import DEFAULT_CSV, SOURCE_UNFILTERED, collected_reviews, load_restaurants
    from utils import (
        DIVIDER, REQUEST_DELAY_SECONDS, REVIEWS_PER_PAGE, ensure_dirs, load_api_key, load_json,
        make_cache_manifest_path, make_raw_page_path, save_json, to_project_relative,
    )

DEFAULT_MAX_REVIEWS: int = 100
DEFAULT_MAX_CREDITS: int = 20
FIRST_PAGE_REVIEWS: int = 10     # an unfiltered page 1 can't ask for num=20

COMPLETE = "complete"
TARGET_MET = "target_met"
IN_PROGRESS = "in_progress"
NOT_STARTED = "not_started"
ENDED_EARLY = "ended_early"
READY = frozenset({COMPLETE, TARGET_MET})
# Google's count also includes some reviews the API never lists: within 10% counts as complete
COMPLETE_SHARE = 0.9
# Pages in a row that end early before the run stops to save credits
MAX_EARLY_ENDS_IN_A_ROW = 3


def state_dir() -> Path:
    return utils.OUTPUT_DIR / "latest_reviews"


def progress_path() -> Path:
    return state_dir() / "progress.json"


def coverage_path() -> Path:
    return state_dir() / "coverage.json"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def _token_of(raw_page: Path) -> Optional[str]:
    try:
        data = load_json(raw_page)
    except (OSError, ValueError):
        return None
    return ((data or {}).get("serpapi_pagination") or {}).get("next_page_token") or None


def seed_from_raw_pages(data_id: str) -> Optional[dict]:
    """
    Where an earlier unfiltered download of *data_id* stopped, from its saved
    raw pages (``<data_id>_pageNNN.json``), or None when there are none.
    Only an unbroken run of pages from page 1 counts.
    """
    page = 0
    while make_raw_page_path(data_id, page + 1).exists():
        page += 1
    if not page:
        return None
    token = _token_of(make_raw_page_path(data_id, page))
    return {"next_page": page + 1, "next_page_token": token, "finished": token is None}


def load_progress(restaurants: list[dict]) -> dict[str, dict]:
    """``{data_id: {"next_page", "next_page_token", "finished"}}`` for every restaurant."""
    saved = {}
    if progress_path().exists():
        try:
            saved = load_json(progress_path()).get("restaurants") or {}
        except (OSError, ValueError, AttributeError):
            print("  [WARN] progress.json is unreadable; rebuilding it from the saved raw pages.")
    progress = {}
    for r in restaurants:
        progress[r["data_id"]] = (saved.get(r["data_id"]) or seed_from_raw_pages(r["data_id"])
                                  or {"next_page": 1, "next_page_token": None, "finished": False})
    return progress


def save_progress(progress: dict[str, dict]) -> None:
    save_json({"saved_at": datetime.datetime.now().isoformat(timespec="seconds"), "restaurants": progress},
              progress_path())


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def unfiltered_counts() -> dict[str, int]:
    """Unique unfiltered reviews saved per data_id (SerpApi and Apify outputs, legacy names included)."""
    with contextlib.redirect_stdout(io.StringIO()):
        by_id = collected_reviews([utils.OUTPUT_DIR, utils.OUTPUT_DIR / "apify"])
    return {did: sum(SOURCE_UNFILTERED in item["sources"] for item in items) for did, items in by_id.items()}


def _google_total(r: dict) -> Optional[int]:
    value = str(r.get("google_review_count") or "").replace(",", "").strip()
    return int(value) if value.isdigit() else None


def _goal(target: int, google_total: Optional[int]) -> int:
    return min(target, google_total) if google_total is not None else target


def coverage_status(collected: int, target: int, google_total: Optional[int], state: dict) -> str:
    if collected >= target:
        return TARGET_MET
    if google_total is not None and google_total < target and collected >= COMPLETE_SHARE * google_total:
        return COMPLETE   # e.g. downloaded through Apify, which keeps no SerpApi page state
    if state.get("finished"):
        return COMPLETE if collected >= COMPLETE_SHARE * _goal(target, google_total) else ENDED_EARLY
    return IN_PROGRESS if state.get("next_page", 1) > 1 else NOT_STARTED


def needs_page(collected: int, target: int, google_total: Optional[int], state: dict,
               retry_ended_early: bool = False) -> bool:
    status = coverage_status(collected, target, google_total, state)
    return status in (IN_PROGRESS, NOT_STARTED) or (status == ENDED_EARLY and retry_ended_early)


def credits_to_target(collected: int, target: int, google_total: Optional[int], state: dict) -> int:
    """Estimated pages still needed to reach the target (0 when ready)."""
    if coverage_status(collected, target, google_total, state) in READY:
        return 0
    goal = _goal(target, google_total)
    if state.get("finished") or state.get("next_page", 1) <= 1:
        # Starts again from page 1, which holds fewer reviews
        return 1 + math.ceil(max(0, goal - FIRST_PAGE_REVIEWS) / REVIEWS_PER_PAGE)
    return math.ceil(max(0, goal - collected) / REVIEWS_PER_PAGE)


def build_coverage(restaurants: list[dict], progress: dict[str, dict], target: int) -> list[dict]:
    counts = unfiltered_counts()
    rows = []
    for r in restaurants:
        state = progress[r["data_id"]]
        collected = counts.get(r["data_id"], 0)
        total = _google_total(r)
        rows.append({
            "restaurant":          r["restaurant"],
            "data_id":             r["data_id"],
            "google_review_count": total,
            "reviews_downloaded":  collected,
            "target":              target,
            "status":              coverage_status(collected, target, total, state),
            "credits_to_target":   credits_to_target(collected, target, total, state),
        })
    return rows


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def archive_raw_pages(data_id: str) -> list[Path]:
    """
    Move the saved unfiltered raw pages (and cache manifest) of *data_id* to
    ``data/raw_data/serpapi/superseded/`` with a timestamp, so starting again
    from page 1 never overwrites them. Returns the new paths.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = utils.RAW_DATA_DIR / "serpapi" / "superseded"
    sources = []
    n = 1
    while make_raw_page_path(data_id, n).exists():
        sources.append(make_raw_page_path(data_id, n))
        n += 1
    manifest = make_cache_manifest_path(data_id)
    if sources and manifest.exists():
        sources.append(manifest)
    moved = []
    for src in sources:
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{src.stem}_{stamp}{src.suffix}"
        src.replace(dest)
        moved.append(dest)
    return moved


def fetch_one_page(api_key: str, r: dict, state: dict, *, verbose: bool = False, fetch_run=None) -> dict:
    """Fetch ONE unfiltered page (1 credit), merge it into the restaurant's file and return the output."""
    if state["next_page"] == 1:
        archive_raw_pages(r["data_id"])
    run = fetch_run or extract_reviews.run_extraction
    kwargs = dict(max_requests=1, delay=0, expected_name=r["restaurant"], fresh=bool(state.get("fresh")),
                  resume_token=state["next_page_token"], first_page=state["next_page"])
    if verbose:
        return run(api_key, r["data_id"], **kwargs)
    with contextlib.redirect_stdout(io.StringIO()):
        return run(api_key, r["data_id"], **kwargs)


def advance(state: dict, out: dict) -> dict:
    """The progress after a page fetch whose result is *out*."""
    status = out.get("collection_status")
    if status in extract_reviews.SUCCESS_STATUSES:
        return {**state, "next_page": state["next_page"] + 1, "next_page_token": None, "finished": True}
    if status == extract_reviews.STATUS_REQUEST_LIMIT:
        token = _token_of(make_raw_page_path(out["data_id"], state["next_page"]))
        if token:
            return {**state, "next_page": state["next_page"] + 1, "next_page_token": token, "finished": False}
    return state   # failed: try the same page again next time


def restart_fresh(state: dict) -> dict:
    """Start an ended_early restaurant again from page 1, bypassing SerpApi's cache."""
    return {"next_page": 1, "next_page_token": None, "finished": False, "fresh": True}


def collect(restaurants: list[dict], api_key: Optional[str], budget: int, target: int, *,
            delay: float = REQUEST_DELAY_SECONDS, verbose: bool = False, fetch_run=None,
            retry_ended_early: bool = False) -> dict:
    """Breadth-first collection within *budget* credits. Returns ``{"credits_used", "stop_reason", "errors"}``."""
    progress = load_progress(restaurants)
    used = 0
    stop_reason: Optional[str] = None
    errors: list[dict] = []
    failed: set[str] = set()
    retried: set[str] = set()
    early_in_a_row = 0
    while not stop_reason:
        counts = unfiltered_counts()
        pending = [r for r in restaurants if r["data_id"] not in failed
                   and needs_page(counts.get(r["data_id"], 0), target, _google_total(r), progress[r["data_id"]],
                                  retry_ended_early and r["data_id"] not in retried)]
        if not pending:
            break
        for r in pending:
            if progress[r["data_id"]].get("finished"):
                progress[r["data_id"]] = restart_fresh(progress[r["data_id"]])
                retried.add(r["data_id"])
        page = min(progress[r["data_id"]]["next_page"] for r in pending)
        work = [r for r in pending if progress[r["data_id"]]["next_page"] == page]
        print(f"\nRound: page {page} for {len(work)} restaurant(s)")
        for r in work:
            if used >= budget:
                stop_reason = f"Credit budget of {budget} reached."
                break
            if used and delay > 0:
                time.sleep(delay)
            state = progress[r["data_id"]]
            out = fetch_one_page(api_key, r, state, verbose=verbose, fetch_run=fetch_run)
            used += out.get("api_requests_used") or 0
            status = out.get("collection_status")
            progress[r["data_id"]] = advance(state, out)
            save_progress(progress)
            note = {extract_reviews.STATUS_REQUEST_LIMIT: "", extract_reviews.STATUS_COMPLETED: "  (all reviews)",
                    extract_reviews.STATUS_NO_REVIEWS: "  (no reviews)"}.get(status, f"  [{status}]")
            if out.get("restaurant_name_matches_expected") is False and page == 1:
                note += f"  [WARN: API returned '{out.get('restaurant_name')}']"
            print(f"  [{used:>3}/{budget}] {r['restaurant'][:34]:34} p{page}: "
                  f"{len(out.get('reviews') or [])} reviews saved{note}")
            if status == extract_reviews.STATUS_QUOTA:
                stop_reason = "SerpApi credits exhausted."
                break
            ended_early = coverage_status(len(out.get("reviews") or []), target, _google_total(r),
                                          progress[r["data_id"]]) == ENDED_EARLY
            early_in_a_row = early_in_a_row + 1 if ended_early else 0
            if early_in_a_row >= MAX_EARLY_ENDS_IN_A_ROW:
                stop_reason = (f"SerpApi ended {early_in_a_row} pages in a row early (no next page), so it "
                               "is serving truncated results right now. Stopped to save credits; try again "
                               "another day.")
                break
            if status in (extract_reviews.STATUS_API_ERROR, extract_reviews.STATUS_PARTIAL) or \
                    progress[r["data_id"]] is state:
                failed.add(r["data_id"])
                errors.append({"restaurant": r["restaurant"], "data_id": r["data_id"], "page": page,
                               "status": status, "error": out.get("api_error")})
                if state["next_page_token"]:
                    # The saved token may have expired: restart this restaurant from page 1 next run
                    progress[r["data_id"]] = {**state, "next_page": 1, "next_page_token": None}
                    save_progress(progress)
    save_progress(progress)
    return {"credits_used": used, "stop_reason": stop_reason, "errors": errors}


def write_report(restaurants: list[dict], target: int, run: Optional[dict] = None) -> list[dict]:
    rows = build_coverage(restaurants, load_progress(restaurants), target)
    save_json({"saved_at": datetime.datetime.now().isoformat(timespec="seconds"), "target": target,
               **({"last_run": run} if run else {}), "restaurants": rows}, coverage_path())
    done = [r for r in rows if r["status"] in READY]
    print(f"\n{DIVIDER}")
    print(f"  Target                : newest {target} reviews per restaurant")
    print(f"  Restaurants ready     : {len(done)} / {len(rows)}"
          f"  ({sum(r['status'] == COMPLETE for r in rows)} with every review)")
    print(f"  Reviews downloaded    : {sum(r['reviews_downloaded'] for r in rows)}")
    print(f"  Credits still needed  : about {sum(r['credits_to_target'] for r in rows)}")
    print(f"  Coverage              : {to_project_relative(coverage_path())}")
    print(DIVIDER)
    pending = [r for r in rows if r not in done]
    stuck = [r for r in rows if r["status"] == ENDED_EARLY]
    if stuck:
        print(f"SerpApi stopped early for {len(stuck)} restaurant(s); fetch them again later with "
              "--retry-ended-early.")
    if pending:
        print(f"Not ready yet ({len(pending)}):")
        for r in pending:
            print(f"  - {r['restaurant'][:40]:40} {r['reviews_downloaded']:>4} / {target}  [{r['status']}]")
    else:
        print("Every restaurant has reached the target: the washroom summary can be built.")
    return rows


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the newest reviews of every listed restaurant.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    parser.add_argument("--max-reviews", type=int, default=DEFAULT_MAX_REVIEWS,
                        help=f"Newest reviews to download per restaurant (default {DEFAULT_MAX_REVIEWS}).")
    parser.add_argument("--max-credits", type=int, default=DEFAULT_MAX_CREDITS,
                        help=f"Max SerpApi searches this run, clamped to the credits left (default {DEFAULT_MAX_CREDITS}).")
    parser.add_argument("--report", action="store_true", help="Only print and save the coverage (0 credits).")
    parser.add_argument("--retry-ended-early", action="store_true",
                        help="Fetch ended_early restaurants again from page 1 (no_cache).")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="Only the first N restaurants.")
    parser.add_argument("--verbose", action="store_true", help="Show the full per-page extraction output.")
    args = parser.parse_args(argv)
    if args.max_reviews < 1:
        parser.error("--max-reviews must be at least 1")
    if args.max_credits < 1:
        parser.error("--max-credits must be at least 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    restaurants = load_restaurants(args.csv)[: args.limit]
    print(f"\n{DIVIDER}\n   LATEST REVIEWS COLLECTOR\n{DIVIDER}\n")
    print(f"Restaurants: {len(restaurants)} from {to_project_relative(args.csv)}")
    if args.report:
        write_report(restaurants, args.max_reviews)
        return 0

    budget = check_credits(args.max_credits)
    if budget is None:
        return 1
    run = collect(restaurants, load_api_key(), budget, args.max_reviews, verbose=args.verbose,
                  retry_ended_early=args.retry_ended_early)
    write_report(restaurants, args.max_reviews, run)
    print(f"\nCredits used: {run['credits_used']} of {budget}")
    if run["stop_reason"]:
        print(f"Stopped: {run['stop_reason']}")
    for e in run["errors"]:
        print(f"  [ERROR] {e['restaurant']} page {e['page']}: {e['status']} "
              f"{(e['error'] or {}).get('message', '')}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
