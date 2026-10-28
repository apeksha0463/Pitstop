"""
scripts/utils.py
================
Shared helpers for the Google Maps Review Extractor.

Covers:
  - API request execution with retry/rate-limit handling
  - Review normalisation (flatten_review)
  - Review deduplication fingerprinting
  - Pagination utility
  - JSON persistence (save_json)
  - Path helpers (project-root-relative, pathlib-based)
  - Input validation

All paths are built with pathlib.Path so the project works on both
Windows and Linux regardless of the working directory, as long as the
script is launched from the project root.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Project layout — all paths relative to the package root
# ---------------------------------------------------------------------------

# Resolving: scripts/ -> google-maps-review-extractor/
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DATA_DIR: Path = DATA_DIR / "raw_data"
EXTRACTED_PAGES_DIR: Path = DATA_DIR / "extracted_pages"
EXTRACTED_IMAGES_DIR: Path = DATA_DIR / "extracted_images"
PROCESSED_DIR: Path = DATA_DIR / "processed"
OUTPUT_DIR: Path = PROJECT_ROOT / "output"


def ensure_dirs() -> None:
    """Create all required data directories if they don't exist."""
    for d in (RAW_DATA_DIR, EXTRACTED_PAGES_DIR, EXTRACTED_IMAGES_DIR, PROCESSED_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Configuration defaults (can be overridden by callers)
# ---------------------------------------------------------------------------

MAX_REQUESTS: int = 10
REQUEST_DELAY_SECONDS: float = 1.5
REQUEST_TIMEOUT_SECONDS: int = 30
MAX_RETRIES: int = 3
SAVE_EVERY_N_PAGES: int = 5

SERPAPI_ENDPOINT: str = "https://serpapi.com/search.json"

DIVIDER: str = "=" * 44


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def load_api_key(env_path: Path | None = None) -> str:
    """
    Load and validate the SerpApi key from the .env file.

    Args:
        env_path: Optional explicit path to the .env file.
                  Defaults to PROJECT_ROOT / '.env'.

    Returns:
        The validated API key string.

    Raises:
        SystemExit on missing or placeholder key.
    """
    dotenv_path = env_path or (PROJECT_ROOT / ".env")
    load_dotenv(dotenv_path=dotenv_path)

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


# ---------------------------------------------------------------------------
# Review helpers
# ---------------------------------------------------------------------------

def make_review_fingerprint(review: dict) -> str:
    """
    Return a stable deduplication key for a review.

    Prefers ``review_id`` when available; falls back to a composite of
    author name, date, and the first 80 characters of review text.
    """
    if review.get("review_id"):
        return str(review["review_id"])
    author = str(review.get("user", {}).get("name", "") or review.get("author", ""))
    date = str(review.get("date", "") or review.get("iso_date", ""))
    text = str(review.get("snippet", "") or review.get("text", ""))[:80]
    return f"{author}|{date}|{text}"


def flatten_review(raw: dict) -> dict:
    """
    Normalise a raw SerpApi review object into a clean, consistent dict.

    Preserves all useful fields and applies safe fallbacks for missing data.
    The API key is never stored in the returned dict.
    """
    user = raw.get("user", {}) or {}
    owner_response = raw.get("owner_answer", None) or raw.get("owner_response", None)
    photos = raw.get("photos", []) or []

    return {
        "review_id":              raw.get("review_id") or raw.get("id") or None,
        "author":                 user.get("name") or raw.get("author") or None,
        "author_url":             user.get("link") or None,
        "author_local_guide":     user.get("local_guide", False),
        "author_reviews_count":   user.get("reviews") or None,
        "rating":                 raw.get("rating") or None,
        "date":                   raw.get("date") or None,
        "iso_date":               raw.get("iso_date") or None,
        "iso_date_of_last_edit":  raw.get("iso_date_of_last_edit") or None,
        "text":                   raw.get("snippet") or raw.get("text") or None,
        "language":               raw.get("language") or None,
        "translated":             raw.get("translated", False),
        "likes":                  raw.get("likes") or 0,
        "owner_response":         owner_response,
        "photos_count":           len(photos),
        "photos": [
            p.get("link") or p.get("url")
            for p in photos
            if isinstance(p, dict)
        ],
        "review_link": raw.get("link") or None,
        "source":      "Google Maps via SerpApi",
    }


def extract_place_info(first_page_data: dict) -> dict:
    """Pull restaurant-level metadata from the first API response."""
    place = first_page_data.get("place_info", {}) or {}
    return {
        "restaurant_name":       place.get("title") or place.get("name") or "Unknown",
        "address":               place.get("address") or None,
        "rating":                place.get("rating") or None,
        "total_reviews_reported": place.get("reviews") or place.get("total_reviews") or None,
        "type":                  place.get("type") or None,
        "phone":                 place.get("phone") or None,
        "website":               place.get("website") or None,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_json(data: dict, filepath: Path | str) -> None:
    """Save *data* as an indented, UTF-8 JSON file at *filepath*."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def make_output_path(data_id: str, restaurant_name: str = "") -> Path:
    """
    Generate a unique, timestamped output path under OUTPUT_DIR.

    Args:
        data_id:          The Google Maps ``data_id``.
        restaurant_name:  Optional restaurant name for a friendlier filename.

    Returns:
        A :class:`pathlib.Path` that does not yet exist.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = data_id.replace(":", "_").replace("/", "_").replace("\\", "_")[:40]

    if restaurant_name:
        safe_name = (
            "".join(c if c.isalnum() or c in " _-" else "_" for c in restaurant_name)
            .strip()
            .replace(" ", "_")[:30]
        )
        filename = f"{safe_name}_{safe_id}_{timestamp}.json"
    else:
        filename = f"restaurant_reviews_{safe_id}_{timestamp}.json"

    return OUTPUT_DIR / filename


def make_raw_page_path(data_id: str, page_num: int) -> Path:
    """Return a path for saving a raw API page response in data/extracted_pages/."""
    EXTRACTED_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = data_id.replace(":", "_").replace("/", "_").replace("\\", "_")[:40]
    return EXTRACTED_PAGES_DIR / f"{safe_id}_page{page_num:03d}.json"


# ---------------------------------------------------------------------------
# HTTP / API layer
# ---------------------------------------------------------------------------

def fetch_page(
    api_key: str,
    params: dict,
    *,
    max_retries: int = MAX_RETRIES,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """
    Fetch a single page from SerpApi with retry and rate-limit handling.

    Args:
        api_key:     The SerpApi key (used only in the request; never stored).
        params:      Query parameters for the SerpApi request.
        max_retries: Number of attempts before raising.
        timeout:     HTTP request timeout in seconds.

    Returns:
        Parsed JSON dict from the API response.

    Raises:
        RuntimeError on unrecoverable failures.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                SERPAPI_ENDPOINT,
                params=params,
                timeout=timeout,
            )

            if response.status_code == 401:
                raise RuntimeError("Invalid API key (HTTP 401). Check your .env file.")
            if response.status_code == 429:
                wait = 30 * attempt
                print(f"  [WARN] Rate limited (HTTP 429). Waiting {wait}s before retry {attempt}/{max_retries}...")
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
                raise RuntimeError("SerpApi returned non-JSON content.")

            # SerpApi wraps quota errors inside the JSON body
            if "error" in data:
                error_msg = data["error"]
                if any(kw in error_msg.lower() for kw in ("credits", "quota", "plan")):
                    raise RuntimeError(
                        "SerpApi credit/quota exhausted. Your free-plan monthly limit may be reached."
                    )
                raise RuntimeError(f"SerpApi API error: {error_msg}")

            return data

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                print(f"  [WARN] Request timed out. Retry {attempt}/{max_retries}...")
                time.sleep(5 * attempt)
            else:
                raise RuntimeError("Request repeatedly timed out. Check your network connection.")

        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                print(f"  [WARN] Connection error. Retry {attempt}/{max_retries}...")
                time.sleep(5 * attempt)
            else:
                raise RuntimeError("Cannot connect to SerpApi. Check your internet connection.")

        except RuntimeError:
            raise

    raise RuntimeError("All retries exhausted.")


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def prompt_data_id() -> str:
    """Prompt the user for a Google Maps data_id and validate it."""
    print("Enter the Google Maps restaurant data_id.")
    print("(Example format: 0x3bae160000000000:0x123456789abcdef0)\n")
    data_id = input("Restaurant data_id: ").strip()
    if not data_id:
        print("[ERROR] data_id cannot be empty. Exiting.")
        sys.exit(1)
    return data_id
