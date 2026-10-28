"""
scripts/extract_reviews.py
==========================
Main entry point for the Pitstop Google Maps Review Extractor.

Accepts a Google Maps restaurant ``data_id`` (entered interactively or
passed as a command-line argument), fetches all available reviews using
SerpApi's ``google_maps_reviews`` engine with automatic pagination,
deduplicates results, and saves the final JSON to ``data/output/``.

Intermediate page-level responses are optionally saved to
``data/extracted_pages/`` for debugging and reproducibility.

Usage (from the repository root):

    python scripts/extract_reviews.py
    # or
    python -m scripts.extract_reviews

To pass the data_id non-interactively:

    python scripts/extract_reviews.py --data-id 0xABCD:0x1234
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repository root and scripts dir are on sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts.utils import (
        DIVIDER,
        MAX_REQUESTS,
        REQUEST_DELAY_SECONDS,
        SAVE_EVERY_N_PAGES,
        extract_place_info,
        fetch_page,
        flatten_review,
        load_api_key,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        prompt_data_id,
        save_json,
    )
except ImportError:
    from utils import (
        DIVIDER,
        MAX_REQUESTS,
        REQUEST_DELAY_SECONDS,
        SAVE_EVERY_N_PAGES,
        extract_place_info,
        fetch_page,
        flatten_review,
        load_api_key,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        prompt_data_id,
        save_json,
    )


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def print_banner() -> None:
    print(f"\n{DIVIDER}")
    print("   GOOGLE MAPS REVIEW EXTRACTOR")
    print("   Powered by SerpApi")
    print(f"{DIVIDER}\n")


# ---------------------------------------------------------------------------
# Progress snapshot
# ---------------------------------------------------------------------------

def _save_progress(
    reviews: list,
    data_id: str,
    place_info: dict,
    pages_fetched: int,
    filepath: Path,
    collection_status: str,
) -> None:
    """Save a partial progress snapshot without disrupting the main loop."""
    try:
        data = {
            "source":                 "SerpApi Google Maps Reviews",
            "data_id":                data_id,
            "restaurant_name":        place_info.get("restaurant_name", "Unknown"),
            "address":                place_info.get("address"),
            "rating":                 place_info.get("rating"),
            "total_reviews_reported": place_info.get("total_reviews_reported"),
            "reviews_collected":      len(reviews),
            "pages_fetched":          pages_fetched,
            "collection_complete":    False,
            "collection_status":      collection_status,
            "extracted_at":           datetime.datetime.now().isoformat(),
            "reviews":                reviews,
        }
        save_json(data, filepath)
    except Exception as exc:
        print(f"  [WARN] Could not save progress snapshot: {exc}")


# ---------------------------------------------------------------------------
# Core extraction loop
# ---------------------------------------------------------------------------

def run_extraction(
    api_key: str,
    data_id: str,
    output_path: Path,
    *,
    max_requests: int = MAX_REQUESTS,
    delay: float = REQUEST_DELAY_SECONDS,
    save_every: int = SAVE_EVERY_N_PAGES,
    save_raw_pages: bool = True,
) -> None:
    """
    Fetch reviews from SerpApi and save them.

    Args:
        api_key:        The SerpApi key.
        data_id:        Google Maps restaurant ``data_id``.
        output_path:    Destination for the final JSON.
        max_requests:   Maximum number of API requests (free-plan guard).
        delay:          Seconds to sleep between requests.
        save_every:     Save a progress snapshot every N pages (0 = off).
        save_raw_pages: If True, write each raw API response to
                        ``data/extracted_pages/``.
    """
    print(f"Restaurant data_id : {data_id}")
    print(f"Output file        : {output_path}")
    print(f"Max API requests   : {max_requests}")
    print(f"\nConnecting to SerpApi...\n")

    all_reviews: list[dict] = []
    seen_fingerprints: set[str] = set()
    pages_fetched = 0
    collection_status = "in_progress"
    place_info: dict = {}
    duplicates_skipped = 0

    base_params = {
        "engine":   "google_maps_reviews",
        "data_id":  data_id,
        "hl":       "en",
        "api_key":  api_key,
    }
    params = dict(base_params)

    try:
        for request_num in range(1, max_requests + 1):
            print(f"Fetching review page {request_num}...")

            try:
                data = fetch_page(api_key, params)
            except RuntimeError as exc:
                print(f"\n[ERROR] {exc}")
                if pages_fetched == 0:
                    print("No reviews were collected. Exiting without saving.")
                    sys.exit(1)
                collection_status = "stopped_on_error"
                break

            pages_fetched += 1

            # Optionally persist the raw page for debugging
            if save_raw_pages:
                raw_path = make_raw_page_path(data_id, pages_fetched)
                save_json(data, raw_path)

            if pages_fetched == 1:
                place_info = extract_place_info(data)
                print(f"\nRestaurant : {place_info['restaurant_name']}")
                if place_info.get("address"):
                    print(f"Address    : {place_info['address']}")
                if place_info.get("rating"):
                    print(f"Rating     : {place_info['rating']} \u2605")
                if place_info.get("total_reviews_reported"):
                    print(f"Total reviews on Google Maps : {place_info['total_reviews_reported']}")
                print()

            raw_reviews = data.get("reviews") or []

            if not raw_reviews:
                print("  -> No reviews found on this page. Stopping.")
                collection_status = "completed"
                break

            new_on_page = 0
            for raw in raw_reviews:
                fp = make_review_fingerprint(raw)
                if fp in seen_fingerprints:
                    duplicates_skipped += 1
                    continue
                seen_fingerprints.add(fp)
                all_reviews.append(flatten_review(raw))
                new_on_page += 1

            print(f"  Reviews this page : {new_on_page}  |  Total collected : {len(all_reviews)}")

            pagination = data.get("serpapi_pagination", {}) or {}
            next_token = pagination.get("next_page_token")

            if not next_token:
                print("\n  -> No next-page token found. All available pages retrieved.")
                collection_status = "completed"
                break

            params = dict(base_params)
            params["next_page_token"] = next_token

            if save_every > 0 and pages_fetched % save_every == 0:
                _save_progress(
                    all_reviews, data_id, place_info, pages_fetched,
                    output_path, collection_status="in_progress",
                )
                print(f"  [Progress saved -> {output_path}]")

            if request_num < max_requests:
                time.sleep(delay)

        else:
            collection_status = "stopped_at_request_limit"
            print(f"\n  -> Reached the configured request limit of {max_requests}.")
            print("  -> Increase MAX_REQUESTS in scripts/utils.py to fetch more reviews.")

    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] User cancelled. Saving collected reviews...")
        collection_status = "interrupted_by_user"

    # -----------------------------------------------------------------------
    # Final save
    # -----------------------------------------------------------------------
    print(f"\nSaving JSON...")

    restaurant_name = place_info.get("restaurant_name", "Unknown")

    output_data = {
        "source":                 "SerpApi Google Maps Reviews",
        "data_id":                data_id,
        "restaurant_name":        restaurant_name,
        "address":                place_info.get("address"),
        "rating":                 place_info.get("rating"),
        "type":                   place_info.get("type"),
        "phone":                  place_info.get("phone"),
        "website":                place_info.get("website"),
        "total_reviews_reported": place_info.get("total_reviews_reported"),
        "reviews_collected":      len(all_reviews),
        "duplicates_skipped":     duplicates_skipped,
        "pages_fetched":          pages_fetched,
        "collection_complete":    collection_status == "completed",
        "collection_status":      collection_status,
        "extracted_at":           datetime.datetime.now().isoformat(),
        "reviews":                all_reviews,
    }

    save_json(output_data, output_path)

    print(f"File saved: {output_path}")
    print(f"\n{DIVIDER}")
    print("  EXTRACTION SUMMARY")
    print(f"{DIVIDER}")
    print(f"  Restaurant        : {restaurant_name}")
    print(f"  Total reviews     : {len(all_reviews)}")
    print(f"  Pages fetched     : {pages_fetched}")
    print(f"  Duplicates skipped: {duplicates_skipped}")
    print(f"  Collection status : {collection_status}")
    print(f"  Output file       : {output_path}")

    if collection_status != "completed":
        print()
        print("  NOTE: Collection is INCOMPLETE.")
        print("  Possible reasons:")
        print("  - You hit the MAX_REQUESTS limit (edit scripts/utils.py to increase).")
        print("  - SerpApi credit quota was reached.")
        print("  - A network/API error occurred mid-way.")
        print("  - Google Maps / SerpApi may not expose all historical reviews.")

    print(f"{DIVIDER}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Google Maps reviews for a restaurant using SerpApi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python scripts/extract_reviews.py\n"
            "  python scripts/extract_reviews.py --data-id 0x3bae160000000000:0x123456789abcdef0\n"
        ),
    )
    parser.add_argument(
        "--data-id",
        metavar="DATA_ID",
        default=None,
        help="Google Maps restaurant data_id (prompted interactively if omitted).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=MAX_REQUESTS,
        help=f"Maximum number of SerpApi requests (default: {MAX_REQUESTS}).",
    )
    parser.add_argument(
        "--no-raw-pages",
        action="store_true",
        default=False,
        help="Skip saving raw API page responses to data/extracted_pages/.",
    )
    return parser.parse_args()


def main() -> None:
    print_banner()
    args = parse_args()

    api_key = load_api_key()
    data_id = args.data_id or prompt_data_id()
    output_path = make_output_path(data_id)

    run_extraction(
        api_key=api_key,
        data_id=data_id,
        output_path=output_path,
        max_requests=args.max_requests,
        save_raw_pages=not args.no_raw_pages,
    )


if __name__ == "__main__":
    main()
