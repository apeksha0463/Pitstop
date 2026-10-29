"""
scripts/collect_washroom_reviews.py
===================================
Collect the washroom-related Google Maps reviews of every restaurant in
data/raw_data/restaurants_route_above_3.5.csv within a SerpApi credit budget.

Per restaurant (``data_id``):
  - if a washroom-like topic (e.g. "clean washrooms") is in its saved review
    topics, the reviews are filtered with that ``topic_id`` (read from saved
    raw pages, 0 credits);
  - otherwise four text searches are made: ``query`` = washroom, restroom,
    toilet, bathroom.

Pages are fetched breadth-first: first page 1 of every search for every
restaurant, then further pages round by round while budget remains (up to
--max-pages-per-search). Each page costs 1 credit and is saved straight
away, so a run stopped by the budget or by exhausted credits loses nothing;
the next run resumes from the saved ``next_page_token``. Restaurants whose
searches are all finished are skipped unless --refresh is passed.

Writes:
  - data/output/washroom/<data_id>_reviews.json   reviews, tagged in matched_by
  - data/output/washroom/collection_summary.json  status per restaurant

Usage (from the repository root):

    python scripts/collect_washroom_reviews.py --max-credits 50
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
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
    from scripts.extract_washroom_mentions import DEFAULT_CSV, is_washroom_related, load_restaurants
    from scripts.utils import (
        DIVIDER,
        REQUEST_DELAY_SECONDS,
        CreditsExhaustedError,
        InvalidApiKeyError,
        ensure_dirs,
        load_api_key,
        load_json,
        load_saved_topics,
        make_output_path,
        review_filter_tag,
        save_json,
        to_project_relative,
        validate_api_key,
    )
except ImportError:
    import extract_reviews
    import utils
    from extract_washroom_mentions import DEFAULT_CSV, is_washroom_related, load_restaurants
    from utils import (
        DIVIDER,
        REQUEST_DELAY_SECONDS,
        CreditsExhaustedError,
        InvalidApiKeyError,
        ensure_dirs,
        load_api_key,
        load_json,
        load_saved_topics,
        make_output_path,
        review_filter_tag,
        save_json,
        to_project_relative,
        validate_api_key,
    )

QUERY_TERMS: tuple[str, ...] = ("washroom", "restroom", "toilet", "bathroom")
DEFAULT_MAX_CREDITS: int = 20
DEFAULT_MAX_PAGES: int = 3

# Per-search states
SEARCH_DONE = "done"            # pagination finished
SEARCH_MORE = "more_pages"      # a next_page_token is saved
SEARCH_TODO = "not_started"     # never fetched (or must restart from page 1)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def washroom_topic(topics: list[dict]) -> Optional[dict]:
    """The washroom-like topic with the most mentions, or None."""
    matches = [t for t in topics if is_washroom_related(t.get("keyword"))]
    return max(matches, key=lambda t: t.get("mentions") or 0) if matches else None


def filter_kwargs(tag: str) -> dict:
    """run_extraction keyword arguments for a ``query:...`` / ``topic:...`` tag."""
    kind, _, value = tag.partition(":")
    return {"topic_id": value} if kind == "topic" else {"query": value}


def load_doc(data_id: str) -> Optional[dict]:
    path = make_output_path(data_id, review_filter="washroom")
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (OSError, ValueError):
        return None


def plan_searches(data_id: str, doc: Optional[dict], refresh: bool = False) -> list[str]:
    """
    The filter tags to collect for *data_id*.

    A restaurant keeps the plan it was started with (so a topic discovered
    later does not trigger new searches) unless *refresh* is set.
    """
    started = [] if refresh else [r.get("review_filter") for r in (doc or {}).get("filter_runs") or []]
    topic_tags = [t for t in started if t and t.startswith("topic:")]
    if topic_tags:
        return [topic_tags[0]]
    if started:
        return [review_filter_tag(query=q) for q in QUERY_TERMS]
    topic = washroom_topic(load_saved_topics(data_id))
    if topic:
        return [review_filter_tag(topic_id=topic["id"])]
    return [review_filter_tag(query=q) for q in QUERY_TERMS]


def search_state(doc: Optional[dict], tag: str, since: Optional[str] = None) -> dict:
    """
    ``{"state", "next_page_token", "next_page"}`` for one search, from the
    latest run recorded in *doc* (runs before *since* are ignored, for --refresh).
    """
    runs = [r for r in (doc or {}).get("filter_runs") or []
            if r.get("review_filter") == tag and not r.get("from_cache")
            and (since is None or (r.get("run_at") or "") >= since)]
    if not runs:
        return {"state": SEARCH_TODO, "next_page_token": None, "next_page": 1}
    last = runs[-1]
    next_page = (last.get("first_page") or 1) + (last.get("pages_fetched") or 0)
    if last.get("collection_status") in extract_reviews.SUCCESS_STATUSES:
        return {"state": SEARCH_DONE, "next_page_token": None, "next_page": next_page}
    if last.get("next_page_token"):
        return {"state": SEARCH_MORE, "next_page_token": last["next_page_token"], "next_page": next_page}
    return {"state": SEARCH_TODO, "next_page_token": None, "next_page": 1}


def needs_page(state: dict, max_pages: int) -> bool:
    return state["state"] != SEARCH_DONE and state["next_page"] <= max_pages


def restaurant_status(doc: Optional[dict], tags: list[str], max_pages: int,
                      since: Optional[str] = None) -> str:
    states = [search_state(doc, t, since) for t in tags]
    if all(s["state"] == SEARCH_TODO for s in states):
        return "not_started"
    if any(needs_page(s, max_pages) for s in states):
        return "incomplete"
    if any(s["state"] == SEARCH_MORE for s in states):
        return "complete_at_page_limit"
    return "complete"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def check_credits(max_credits: int) -> Optional[int]:
    """Budget for this run: *max_credits* clamped to the credits left (free account check)."""
    print("Checking SerpApi key (account check, uses no search credits)...")
    try:
        account = validate_api_key(load_api_key())
    except (InvalidApiKeyError, CreditsExhaustedError) as exc:
        print(f"[ERROR] {exc}")
        return None
    if account is None:
        print("  [WARN] Could not read the remaining credits; using --max-credits as the budget.")
        return max_credits
    left = account.get("total_searches_left")
    print(f"  Key OK. Plan: {account.get('plan_name') or 'unknown'} | searches left: {left}")
    if isinstance(left, int) and left < max_credits:
        print(f"  Budget clamped from {max_credits} to {left} (remaining credits).")
        return left
    return max_credits


def fetch_one_page(api_key: str, data_id: str, tag: str, state: dict, *,
                   restaurant: str, verbose: bool = False, fetch_run=None) -> dict:
    """Fetch ONE page of one filtered search (1 credit) and merge it into the restaurant's file."""
    run = fetch_run or extract_reviews.run_extraction
    kwargs = dict(
        max_requests=1, delay=0, expected_name=restaurant,
        resume_token=state["next_page_token"], first_page=state["next_page"],
        **filter_kwargs(tag),
    )
    if verbose:
        return run(api_key, data_id, **kwargs)
    with contextlib.redirect_stdout(io.StringIO()):
        return run(api_key, data_id, **kwargs)


def collect(restaurants: list[dict], api_key: Optional[str], budget: int, *,
            max_pages: int = DEFAULT_MAX_PAGES, refresh: bool = False,
            delay: float = REQUEST_DELAY_SECONDS, verbose: bool = False,
            fetch_run=None) -> dict:
    """
    Breadth-first collection within *budget* credits.

    Returns ``{"credits_used", "stop_reason", "restaurants": [status rows]}``.
    """
    since = datetime.datetime.now().isoformat(timespec="seconds") if refresh else None
    plans = {r["data_id"]: plan_searches(r["data_id"], load_doc(r["data_id"]), refresh) for r in restaurants}
    skipped = [r for r in restaurants
               if restaurant_status(load_doc(r["data_id"]), plans[r["data_id"]], max_pages, since)
               .startswith("complete")]
    if skipped:
        print(f"Skipping {len(skipped)} restaurant(s) already complete (use --refresh to fetch again).")
    active = [r for r in restaurants if r not in skipped]

    used = 0
    stop_reason: Optional[str] = None
    failed: set[tuple[str, str]] = set()   # searches that hit a non-credit API error this run
    for round_num in range(1, max_pages + 1):
        work = [(r, tag) for r in active for tag in plans[r["data_id"]]
                if (r["data_id"], tag) not in failed
                and needs_page(search_state(load_doc(r["data_id"]), tag, since), max_pages)
                and search_state(load_doc(r["data_id"]), tag, since)["next_page"] == round_num]
        if not work:
            continue
        print(f"\nRound {round_num}: {len(work)} search page(s) to fetch")
        for r, tag in work:
            if used >= budget:
                stop_reason = f"Credit budget of {budget} reached."
                break
            state = search_state(load_doc(r["data_id"]), tag, since)
            if used and delay > 0:
                time.sleep(delay)
            out = fetch_one_page(api_key, r["data_id"], tag, state, restaurant=r["restaurant"],
                                 verbose=verbose, fetch_run=fetch_run)
            run = (out.get("filter_runs") or [{}])[-1]
            used += run.get("requests_used") or 0
            status = out.get("collection_status")
            more = " (more pages)" if status == extract_reviews.STATUS_REQUEST_LIMIT else ""
            print(f"  [{used:>3}/{budget}] {r['restaurant'][:32]:32} {tag:20} p{state['next_page']}: "
                  f"{run.get('reviews_returned', 0):>2} reviews{more}"
                  + (f"  [{status}]" if status in extract_reviews.FAILURE_STATUSES
                     or status == extract_reviews.STATUS_PARTIAL else ""))
            if status == extract_reviews.STATUS_QUOTA:
                stop_reason = "SerpApi credits exhausted."
                break
            if status in (extract_reviews.STATUS_API_ERROR, extract_reviews.STATUS_PARTIAL):
                failed.add((r["data_id"], tag))
        if stop_reason:
            break

    rows = []
    for r in restaurants:
        doc = load_doc(r["data_id"])
        tags = plans[r["data_id"]]
        rows.append({
            "restaurant": r["restaurant"],
            "data_id":    r["data_id"],
            "searches":   tags,
            "status":     restaurant_status(doc, tags, max_pages, since if r not in skipped else None),
            "reviews_collected": len((doc or {}).get("reviews") or []),
        })
    return {"credits_used": used, "budget": budget, "stop_reason": stop_reason,
            "max_pages_per_search": max_pages, "restaurants": rows}


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect washroom-related reviews for every listed restaurant.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    parser.add_argument("--max-credits", type=int, default=DEFAULT_MAX_CREDITS,
                        help=f"Max SerpApi searches this run, clamped to the credits left (default {DEFAULT_MAX_CREDITS}).")
    parser.add_argument("--max-pages-per-search", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"Max pages (20 reviews each) per search (default {DEFAULT_MAX_PAGES}).")
    parser.add_argument("--refresh", action="store_true",
                        help="Fetch completed restaurants again, from page 1.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Only the first N restaurants of the CSV.")
    parser.add_argument("--verbose", action="store_true", help="Show the full per-page extraction output.")
    args = parser.parse_args(argv)
    if args.max_credits < 1:
        parser.error("--max-credits must be at least 1")
    if args.max_pages_per_search < 1:
        parser.error("--max-pages-per-search must be at least 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    ensure_dirs()
    restaurants = load_restaurants(args.csv)[: args.limit]
    print(f"\n{DIVIDER}\n   WASHROOM REVIEW COLLECTOR\n{DIVIDER}\n")
    print(f"Restaurants: {len(restaurants)} from {to_project_relative(args.csv)}")

    budget = check_credits(args.max_credits)
    if budget is None:
        return 1
    result = collect(restaurants, load_api_key(), budget, max_pages=args.max_pages_per_search,
                     refresh=args.refresh, verbose=args.verbose)

    summary_path = utils.filtered_dir(utils.OUTPUT_DIR, "washroom") / "collection_summary.json"
    save_json({**result, "saved_at": datetime.datetime.now().isoformat(timespec="seconds")}, summary_path)

    rows = result["restaurants"]
    pending = [r for r in rows if not r["status"].startswith("complete")]
    print(f"\n{DIVIDER}")
    print(f"  Credits used          : {result['credits_used']} of {result['budget']}")
    print(f"  Complete              : {len(rows) - len(pending)} / {len(rows)} restaurants")
    print(f"  Reviews collected     : {sum(r['reviews_collected'] for r in rows)}")
    print(f"  Summary               : {to_project_relative(summary_path)}")
    print(DIVIDER)
    if result["stop_reason"]:
        print(f"Stopped: {result['stop_reason']}")
    if pending:
        print(f"Still need fetching ({len(pending)}):")
        for r in pending:
            print(f"  - {r['restaurant']} ({r['data_id']}) [{r['status']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
