"""
Google Maps Review Extractor using SerpApi
==========================================
Fetches all available reviews for a restaurant identified by its Google Maps data_id.
Uses the SerpApi Google Maps Reviews API with automatic pagination.

Usage:
    python get_reviews.py

Requirements:
    pip install -r requirements.txt
    Create a .env file with: SERPAPI_API_KEY=your_key_here
"""

import json
import os
import sys
import time
import datetime
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration -- adjust these as needed
# ---------------------------------------------------------------------------

# Maximum number of SerpApi requests to make (protects free-plan credits).
# Each request typically returns ~10 reviews. 10 requests = ~100 reviews max.
MAX_REQUESTS = 10

# Seconds to wait between requests (be polite to the API; also helps avoid rate limits)
REQUEST_DELAY_SECONDS = 1.5

# HTTP request timeout in seconds
REQUEST_TIMEOUT_SECONDS = 30

# Max retries on transient network errors before giving up
MAX_RETRIES = 3

# SerpApi endpoint
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# Save progress every N pages (0 = only save at the end)
SAVE_EVERY_N_PAGES = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIVIDER = "=" * 44


def print_banner():
    print(f"\n{DIVIDER}")
    print("   GOOGLE MAPS REVIEW EXTRACTOR")
    print(f"   Powered by SerpApi")
    print(f"{DIVIDER}\n")


def load_api_key() -> str:
    """Load and validate the SerpApi key from the .env file."""
    load_dotenv()
    key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not key:
        print("[ERROR] SERPAPI_API_KEY not found.")
        print("  -> Create a .env file with: SERPAPI_API_KEY=your_key_here")
        print("  -> See .env.example for reference.")
        sys.exit(1)
    if key == "YOUR_PRIVATE_API_KEY_HERE":
        print("[ERROR] Replace the placeholder in .env with your real SerpApi key.")
        sys.exit(1)
    return key


def get_data_id() -> str:
    """Prompt the user for a Google Maps data_id and validate it."""
    print("Enter the Google Maps restaurant data_id.")
    print("(Example format: 0x3bae160000000000:0x123456789abcdef0)\n")
    data_id = input("Restaurant data_id: ").strip()
    if not data_id:
        print("[ERROR] data_id cannot be empty. Exiting.")
        sys.exit(1)
    return data_id


def make_output_path(data_id: str) -> str:
    """Generate a unique output file path inside the output/ directory."""
    os.makedirs("output", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = data_id.replace(":", "_").replace("/", "_").replace("\\", "_")[:40]
    filename = f"restaurant_reviews_{safe_id}_{timestamp}.json"
    return os.path.join("output", filename)


def save_json(data: dict, filepath: str) -> None:
    """Save a dictionary as a formatted UTF-8 JSON file."""
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_review_fingerprint(review: dict) -> str:
    """
    Return a stable deduplication key for a review.
    Prefer the review_id field; fall back to author + date + first 80 chars of text.
    """
    if review.get("review_id"):
        return str(review["review_id"])
    author = str(review.get("user", {}).get("name", "") or review.get("author", ""))
    date = str(review.get("date", "") or review.get("iso_date", ""))
    text = str(review.get("snippet", "") or review.get("text", ""))[:80]
    return f"{author}|{date}|{text}"


def flatten_review(raw: dict) -> dict:
    """
    Normalize a raw SerpApi review object into a clean, consistent dict.
    Preserves all useful fields while applying safe fallbacks.
    """
    user = raw.get("user", {}) or {}
    owner_response = raw.get("owner_answer", None) or raw.get("owner_response", None)
    photos = raw.get("photos", []) or []

    return {
        "review_id": raw.get("review_id") or raw.get("id") or None,
        "author": user.get("name") or raw.get("author") or None,
        "author_url": user.get("link") or None,
        "author_local_guide": user.get("local_guide", False),
        "author_reviews_count": user.get("reviews") or None,
        "rating": raw.get("rating") or None,
        "date": raw.get("date") or None,
        "iso_date": raw.get("iso_date") or None,
        "iso_date_of_last_edit": raw.get("iso_date_of_last_edit") or None,
        "text": raw.get("snippet") or raw.get("text") or None,
        "language": raw.get("language") or None,
        "translated": raw.get("translated", False),
        "likes": raw.get("likes") or 0,
        "owner_response": owner_response,
        "photos_count": len(photos),
        "photos": [p.get("link") or p.get("url") for p in photos if isinstance(p, dict)],
        "review_link": raw.get("link") or None,
        "source": "Google Maps via SerpApi",
    }


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------

def fetch_page(api_key: str, params: dict) -> dict:
    """
    Fetch a single page from SerpApi with retry logic.
    Raises RuntimeError on unrecoverable failures.
    Returns the parsed JSON dict on success.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                SERPAPI_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS
            )

            if response.status_code == 401:
                raise RuntimeError("Invalid API key (HTTP 401). Check your .env file.")
            if response.status_code == 429:
                wait = 30 * attempt
                print(f"  [WARN] Rate limited (HTTP 429). Waiting {wait}s before retry {attempt}/{MAX_RETRIES}...")
                time.sleep(wait)
                continue
            if response.status_code == 400:
                raise RuntimeError(
                    "Bad request (HTTP 400). The data_id may be invalid or malformed."
                )
            if response.status_code >= 500:
                wait = 10 * attempt
                print(f"  [WARN] Server error (HTTP {response.status_code}). Retrying in {wait}s...")
                time.sleep(wait)
                continue

            response.raise_for_status()

            try:
                data = response.json()
            except ValueError:
                raise RuntimeError("SerpApi returned non-JSON content. This is unexpected.")

            # SerpApi wraps quota errors inside the JSON body
            if "error" in data:
                error_msg = data["error"]
                if "credits" in error_msg.lower() or "quota" in error_msg.lower() or "plan" in error_msg.lower():
                    raise RuntimeError(
                        "SerpApi credit/quota exhausted. Your free-plan monthly limit may be reached."
                    )
                raise RuntimeError(f"SerpApi API error: {error_msg}")

            return data

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                print(f"  [WARN] Request timed out. Retry {attempt}/{MAX_RETRIES}...")
                time.sleep(5 * attempt)
            else:
                raise RuntimeError("Request repeatedly timed out. Check your network connection.")

        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES:
                print(f"  [WARN] Connection error. Retry {attempt}/{MAX_RETRIES}...")
                time.sleep(5 * attempt)
            else:
                raise RuntimeError("Cannot connect to SerpApi. Check your internet connection.")

        except RuntimeError:
            raise

    raise RuntimeError("All retries exhausted.")


def extract_place_info(first_page_data: dict) -> dict:
    """Pull restaurant-level metadata from the first API response."""
    place = first_page_data.get("place_info", {}) or {}
    return {
        "restaurant_name": place.get("title") or place.get("name") or "Unknown",
        "address": place.get("address") or None,
        "rating": place.get("rating") or None,
        "total_reviews_reported": place.get("reviews") or place.get("total_reviews") or None,
        "type": place.get("type") or None,
        "phone": place.get("phone") or None,
        "website": place.get("website") or None,
    }


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

def run_extraction(api_key: str, data_id: str, output_path: str) -> None:
    """
    Core extraction loop:
    1. Fetch pages of reviews with pagination.
    2. Deduplicate reviews.
    3. Periodically save progress.
    4. Write final JSON.
    """
    print(f"Restaurant data_id : {data_id}")
    print(f"Output file        : {output_path}")
    print(f"Max API requests   : {MAX_REQUESTS}")
    print(f"\nConnecting to SerpApi...\n")

    all_reviews = []
    seen_fingerprints = set()
    pages_fetched = 0
    collection_status = "in_progress"
    place_info = {}
    duplicates_skipped = 0

    base_params = {
        "engine": "google_maps_reviews",
        "data_id": data_id,
        "hl": "en",
        "api_key": api_key,
    }

    params = dict(base_params)

    try:
        for request_num in range(1, MAX_REQUESTS + 1):
            print(f"Fetching review page {request_num}...")

            try:
                data = fetch_page(api_key, params)
            except RuntimeError as e:
                print(f"\n[ERROR] {e}")
                if pages_fetched == 0:
                    print("No reviews were collected. Exiting without saving.")
                    sys.exit(1)
                collection_status = "stopped_on_error"
                break

            pages_fetched += 1

            if pages_fetched == 1:
                place_info = extract_place_info(data)
                print(f"\nRestaurant : {place_info['restaurant_name']}")
                if place_info.get("address"):
                    print(f"Address    : {place_info['address']}")
                if place_info.get("rating"):
                    print(f"Rating     : {place_info['rating']} *")
                if place_info.get("total_reviews_reported"):
                    print(f"Total reviews on Google Maps: {place_info['total_reviews_reported']}")
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

            if SAVE_EVERY_N_PAGES > 0 and pages_fetched % SAVE_EVERY_N_PAGES == 0:
                _save_progress(
                    all_reviews, data_id, place_info, pages_fetched,
                    output_path, collection_status="in_progress"
                )
                print(f"  [Progress saved -> {output_path}]")

            if request_num < MAX_REQUESTS:
                time.sleep(REQUEST_DELAY_SECONDS)

        else:
            collection_status = "stopped_at_request_limit"
            print(f"\n  -> Reached the configured request limit of {MAX_REQUESTS}.")
            print(f"  -> Increase MAX_REQUESTS in get_reviews.py to fetch more reviews.")

    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] User cancelled. Saving collected reviews...")
        collection_status = "interrupted_by_user"

    # Final save
    print(f"\nSaving JSON...")

    output_data = {
        "source": "SerpApi Google Maps Reviews",
        "data_id": data_id,
        "restaurant_name": place_info.get("restaurant_name", "Unknown"),
        "address": place_info.get("address"),
        "rating": place_info.get("rating"),
        "type": place_info.get("type"),
        "phone": place_info.get("phone"),
        "website": place_info.get("website"),
        "total_reviews_reported": place_info.get("total_reviews_reported"),
        "reviews_collected": len(all_reviews),
        "duplicates_skipped": duplicates_skipped,
        "pages_fetched": pages_fetched,
        "collection_complete": collection_status == "completed",
        "collection_status": collection_status,
        "extracted_at": datetime.datetime.now().isoformat(),
        "reviews": all_reviews,
    }

    save_json(output_data, output_path)

    print(f"File saved: {output_path}")
    print(f"\n{DIVIDER}")
    print(f"  EXTRACTION SUMMARY")
    print(f"{DIVIDER}")
    print(f"  Restaurant        : {place_info.get('restaurant_name', 'Unknown')}")
    print(f"  Total reviews     : {len(all_reviews)}")
    print(f"  Pages fetched     : {pages_fetched}")
    print(f"  Duplicates skipped: {duplicates_skipped}")
    print(f"  Collection status : {collection_status}")
    print(f"  Output file       : {output_path}")

    if collection_status != "completed":
        print()
        print("  NOTE: Collection is INCOMPLETE.")
        print("  Possible reasons:")
        print("  - You hit the MAX_REQUESTS limit (edit get_reviews.py to increase).")
        print("  - SerpApi credit quota was reached.")
        print("  - A network/API error occurred mid-way.")
        print("  - Google Maps / SerpApi may not expose all historical reviews.")

    print(f"{DIVIDER}\n")


def _save_progress(
    reviews,
    data_id,
    place_info,
    pages_fetched,
    filepath,
    collection_status,
):
    """Internal helper: save a progress snapshot without disrupting the main flow."""
    try:
        data = {
            "source": "SerpApi Google Maps Reviews",
            "data_id": data_id,
            "restaurant_name": place_info.get("restaurant_name", "Unknown"),
            "address": place_info.get("address"),
            "rating": place_info.get("rating"),
            "total_reviews_reported": place_info.get("total_reviews_reported"),
            "reviews_collected": len(reviews),
            "pages_fetched": pages_fetched,
            "collection_complete": False,
            "collection_status": collection_status,
            "extracted_at": datetime.datetime.now().isoformat(),
            "reviews": reviews,
        }
        save_json(data, filepath)
    except Exception as e:
        print(f"  [WARN] Could not save progress snapshot: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print_banner()
    api_key = load_api_key()
    data_id = get_data_id()
    output_path = make_output_path(data_id)
    run_extraction(api_key, data_id, output_path)


if __name__ == "__main__":
    main()
