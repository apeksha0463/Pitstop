"""
scripts/utils.py
================
Shared helpers for the Pitstop Google Maps Review / Review-Image Extractor.

Covers:
  - Project paths (pathlib-based, relative to the repository root)
  - API key loading (sources/.env) and validation via SerpApi's free
    Account API (which does not consume search credits)
  - SerpApi request execution with retry/rate-limit handling
  - Secret redaction for anything written to disk
  - Review normalisation, review-image URL extraction and deduplication
  - JSON persistence and saved-page (cache) helpers
  - Input validation

All paths are built with pathlib.Path so the project works reliably
from Windows PowerShell, VS Code or any other working directory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Project layout — all paths relative to the repository root
# ---------------------------------------------------------------------------

# Resolving: scripts/ -> Pitstop/ (repository root)
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"
DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DATA_DIR: Path = DATA_DIR / "raw_data"
EXTRACTED_PAGES_DIR: Path = DATA_DIR / "extracted_pages"
EXTRACTED_IMAGES_DIR: Path = DATA_DIR / "extracted_images"
PROCESSED_DIR: Path = DATA_DIR / "processed"
OUTPUT_DIR: Path = DATA_DIR / "output"
# Filtered (--query / --topic-id) runs are kept apart from the full review history,
# in a "washroom" sub-folder of each data folder (see filtered_dir()).
FILTERED_SUBDIR: str = "washroom"
SOURCES_DIR: Path = PROJECT_ROOT / "sources"
DEFAULT_ENV_FILE: Path = SOURCES_DIR / ".env"


def ensure_dirs() -> None:
    """Create all required data and sources directories if they don't exist."""
    for d in (RAW_DATA_DIR, EXTRACTED_PAGES_DIR, EXTRACTED_IMAGES_DIR, PROCESSED_DIR, OUTPUT_DIR, SOURCES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def to_project_relative(path: Path | str) -> str:
    """
    Return *path* as a forward-slash string relative to the project root
    (e.g. ``data/extracted_images/x.jpg``). Paths outside the project are
    returned unchanged so nothing is silently misreported.
    """
    p = Path(path).resolve()
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Configuration defaults (can be overridden by callers / CLI flags)
# ---------------------------------------------------------------------------

# Conservative default to protect SerpApi free-plan credits.
# 1 request = 1 search credit. Override with --max-requests.
MAX_REQUESTS: int = 5
REQUEST_DELAY_SECONDS: float = 1.5
REQUEST_TIMEOUT_SECONDS: int = 30
MAX_RETRIES: int = 3
# Progress is saved after every page; kept for backwards compatibility.
SAVE_EVERY_N_PAGES: int = 1
# Reviews per page for paginated requests (SerpApi allows 1-20).
REVIEWS_PER_PAGE: int = 20

SERPAPI_ENDPOINT: str = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_ENDPOINT: str = "https://serpapi.com/account.json"

DIVIDER: str = "=" * 44

_PLACEHOLDER_KEYS = {
    "YOUR_PRIVATE_API_KEY_HERE",
    "PASTE_YOUR_NEW_KEY_HERE",
    "YOUR_SERPAPI_KEY_HERE",
    "YOUR_ACTUAL_SERPAPI_KEY_HERE",
}


# ---------------------------------------------------------------------------
# Errors (messages never include the API key)
# ---------------------------------------------------------------------------

class SerpApiError(RuntimeError):
    """Unrecoverable SerpApi failure."""


class InvalidApiKeyError(SerpApiError):
    """The API key was rejected by SerpApi."""


class CreditsExhaustedError(SerpApiError):
    """The SerpApi account has no searches left (or hit its plan limit)."""


_CREDIT_KEYWORDS = ("run out of searches", "credits", "quota", "plan", "searches left", "limit")
_INVALID_KEY_KEYWORDS = ("invalid api key", "api key", "unauthorized")
_NO_RESULTS_KEYWORDS = ("hasn't returned any results", "has not returned any results", "no results")


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def load_api_key(env_path: Path | None = None) -> str:
    """
    Load and sanity-check the SerpApi key from ``sources/.env``.

    The file is read with ``python-dotenv`` (BOM-tolerant). A value in
    ``sources/.env`` takes precedence over any stale ``SERPAPI_API_KEY``
    already in the process environment. The key is never printed.

    Raises:
        SystemExit on a missing, placeholder or malformed key.
    """
    dotenv_path = Path(env_path) if env_path is not None else DEFAULT_ENV_FILE

    key = ""
    if dotenv_path.exists():
        values = dotenv_values(dotenv_path, encoding="utf-8-sig")
        key = (values.get("SERPAPI_API_KEY") or "").strip()
    if not key:
        key = os.environ.get("SERPAPI_API_KEY", "").strip()

    if not key:
        print("[ERROR] SERPAPI_API_KEY not found.")
        print(f"  -> Create the file: {to_project_relative(DEFAULT_ENV_FILE)}")
        print("  -> Format: SERPAPI_API_KEY=your_key_here")
        print("  -> See scripts/.env.example for reference.")
        sys.exit(1)
    if key.upper() in _PLACEHOLDER_KEYS or "PASTE" in key.upper() or key.upper().startswith("YOUR_"):
        print("[ERROR] Replace the placeholder in sources/.env with your real SerpApi key.")
        sys.exit(1)
    if len(key) < 20 or re.search(r"\s", key):
        print("[ERROR] The SERPAPI_API_KEY in sources/.env looks malformed "
              "(too short or contains spaces). Copy it again from "
              "https://serpapi.com/manage-api-key")
        sys.exit(1)
    return key


def validate_api_key(api_key: str, *, timeout: int = 15) -> dict | None:
    """
    Check the key against SerpApi's Account API (does NOT use search credits).

    Returns:
        A dict with non-secret account info (plan name, searches left), or
        ``None`` if the account endpoint could not be reached (network issue)
        so the caller may decide whether to continue.

    Raises:
        InvalidApiKeyError:    the key was rejected.
        CreditsExhaustedError: the account has zero searches left.
    """
    try:
        resp = requests.get(SERPAPI_ACCOUNT_ENDPOINT, params={"api_key": api_key}, timeout=timeout)
    except requests.exceptions.RequestException:
        # Exception text may contain the request URL (with the key) — never print it.
        return None

    try:
        body = resp.json()
    except ValueError:
        body = {}

    error = str(body.get("error") or "")
    if resp.status_code == 401 or any(k in error.lower() for k in _INVALID_KEY_KEYWORDS):
        raise InvalidApiKeyError(
            "SerpApi rejected the API key in sources/.env (invalid or revoked). "
            "Check it at https://serpapi.com/manage-api-key"
        )
    if resp.status_code != 200 or error:
        return None

    info = {
        "plan_name":           body.get("plan_name"),
        "searches_per_month":  body.get("searches_per_month"),
        "plan_searches_left":  body.get("plan_searches_left"),
        "total_searches_left": body.get("total_searches_left"),
        "this_hour_searches":  body.get("this_hour_searches"),
        "account_rate_limit_per_hour": body.get("account_rate_limit_per_hour"),
    }
    left = info["total_searches_left"]
    if isinstance(left, int) and left <= 0:
        raise CreditsExhaustedError(
            "Your SerpApi account has no searches left this month. "
            "Wait for the monthly reset or upgrade the plan."
        )
    return info


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"


def redact_secrets(obj: Any, secret: str | None = None) -> Any:
    """
    Return a deep copy of *obj* with every ``api_key`` field removed and any
    occurrence of *secret* inside string values replaced by ``[REDACTED]``.
    Use on every API payload before it is written to disk.
    """
    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items() if str(k).lower() != "api_key"}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, str):
            if secret and secret in o:
                o = o.replace(secret, REDACTED)
            return re.sub(r"(api_key=)[^&\s\"']+", r"\1" + REDACTED, o)
        return o

    return _walk(copy.deepcopy(obj))


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
    user = review.get("user") if isinstance(review.get("user"), dict) else {}
    author = str(user.get("name", "") or review.get("author", ""))
    date = str(review.get("date", "") or review.get("iso_date", ""))
    text = str(review.get("snippet", "") or review.get("text", ""))[:80]
    return f"{author}|{date}|{text}"


# image_type values: the three image categories are never mixed.
IMAGE_TYPE_MAIN = "main_restaurant_photo"
IMAGE_TYPE_RESTAURANT = "restaurant_photo"
IMAGE_TYPE_REVIEW = "customer_review_photo"

# Keys that may hold an image URL when an image entry is an object.
_IMAGE_URL_KEYS = ("image", "url", "link", "original", "src", "image_url", "thumbnail")
# Non-URL metadata worth keeping when present on an image object.
_IMAGE_META_KEYS = ("title", "caption", "width", "height", "thumbnail", "type", "source")


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def extract_review_images(raw: dict) -> list[dict]:
    """
    Extract the images attached to ONE review, whatever shape SerpApi used.

    Handles ``images`` (current API: list of URL strings) and the legacy
    ``photos`` field, and tolerates a single string, a single object, lists
    of objects with differing keys, ``None`` and junk entries.

    Returns a list of ``{"image_url": ..., "image_index": n, ...metadata}``
    dicts, deduplicated by URL within the review. URLs are never invented:
    entries without a usable http(s) URL are dropped.
    """
    entries: list[Any] = []
    for field in ("images", "photos"):
        value = raw.get(field)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            entries.extend(value)
        else:
            entries.append(value)

    images: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        url: str | None = None
        meta: dict = {}
        if isinstance(entry, str):
            url = entry.strip()
        elif isinstance(entry, dict):
            for k in _IMAGE_URL_KEYS:
                if _is_http_url(entry.get(k)):
                    url = entry[k].strip()
                    break
            meta = {
                k: entry[k] for k in _IMAGE_META_KEYS
                if k in entry and entry[k] not in (None, "") and entry[k] != url
            }
        if not _is_http_url(url) or url in seen:
            continue
        seen.add(url)
        images.append({"image_url": url, "image_index": len(images) + 1,
                       "image_type": IMAGE_TYPE_REVIEW, **meta})
    return images


def flatten_review(raw: dict) -> dict:
    """
    Normalise a raw SerpApi review object into a clean, consistent dict.

    The original review text is preserved verbatim. ``images`` holds only
    images attached to this review (never general restaurant photos); each
    entry later gains ``local_path`` / ``download_status`` from the image
    downloader. The API key is never stored in the returned dict.
    """
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    owner_response = raw.get("owner_answer", None) or raw.get("owner_response", None)
    images = extract_review_images(raw)
    extracted = raw.get("extracted_snippet") if isinstance(raw.get("extracted_snippet"), dict) else {}

    return {
        "review_id":              raw.get("review_id") or raw.get("id") or None,
        "author":                 user.get("name") or raw.get("author") or None,
        "author_url":             user.get("link") or None,
        "author_local_guide":     user.get("local_guide", False),
        "author_reviews_count":   user.get("reviews") or None,
        "author_photos_count":    user.get("photos") or None,
        "rating":                 raw.get("rating") if raw.get("rating") is not None else None,
        "date":                   raw.get("date") or None,
        "iso_date":               raw.get("iso_date") or None,
        "iso_date_of_last_edit":  raw.get("iso_date_of_last_edit") or None,
        "text":                   raw.get("snippet") or raw.get("text") or None,
        "text_original_language": extracted.get("original") or None,
        "language":               raw.get("language") or None,
        "translated":             raw.get("translated", False),
        "likes":                  raw.get("likes") or 0,
        "details":                raw.get("details") if isinstance(raw.get("details"), dict) else None,
        "owner_response":         owner_response,
        "review_link":            raw.get("link") or None,
        "images_count":           len(images),
        "images":                 images,
        "source":                 "Google Maps via SerpApi",
    }


# place_info.type values that are parts of a building, not reviewable businesses.
_NON_LISTING_PLACE_TYPES = {"level", "floor", "building level", "building floor"}


def extract_place_info(first_page_data: dict) -> dict:
    """
    Pull restaurant-level metadata from the first API response.

    ``restaurant_name`` is taken verbatim from ``place_info.title`` (the
    field SerpApi documents); ``name_source`` records where it came from,
    and ``place_warning`` explains when the data_id does not look like a
    restaurant listing (e.g. a building floor called "G").
    """
    place = first_page_data.get("place_info", {}) or {}
    search_info = first_page_data.get("search_information", {}) or {}
    if place.get("title"):
        name, source = place["title"], "place_info.title"
    elif place.get("name"):
        name, source = place["name"], "place_info.name"
    else:
        name, source = "Unknown", None
    info = {
        "restaurant_name":        name,
        "name_source":            source,
        "address":                place.get("address") or None,
        "rating":                 place.get("rating") or None,
        "total_reviews_reported": place.get("reviews") or place.get("total_reviews") or None,
        "type":                   place.get("type") or None,
        "phone":                  place.get("phone") or None,
        "website":                place.get("website") or None,
        "reviews_results_state":  search_info.get("reviews_results_state") or None,
    }
    info["place_warning"] = place_warning(info)
    return info


def place_warning(info: dict) -> str | None:
    """Explain why the place returned for a data_id may not be the intended restaurant."""
    if info.get("name_source") is None:
        return "The response has no place_info title; the restaurant name is NOT verified."
    ptype = str(info.get("type") or "").strip().lower()
    if ptype in _NON_LISTING_PLACE_TYPES:
        return (
            f"SerpApi resolved this data_id to '{info['restaurant_name']}' of type "
            f"'{info.get('type')}', a floor/level of a building, not a restaurant listing. "
            "Google Maps URLs for places inside a building can carry the floor's ID. "
            "Open the restaurant's own listing and copy the 0x...:0x... value after '!1s' in its URL."
        )
    if not (info.get("address") or info.get("rating") or info.get("total_reviews_reported")):
        return (
            f"'{info['restaurant_name']}' has no address, rating or review count in the response; "
            "it may not be a business listing. Check the data_id."
        )
    return None


def name_matches(actual: str | None, expected: str | None) -> bool | None:
    """Case-insensitive check that *actual* is (or contains) *expected*; None when nothing to compare."""
    if not expected:
        return None
    a = re.sub(r"\s+", " ", str(actual or "")).strip().casefold()
    e = re.sub(r"\s+", " ", expected).strip().casefold()
    return bool(a) and (a == e or e in a)


# ---------------------------------------------------------------------------
# Restaurant (listing) photo helpers — SerpApi Google Maps Place Results
# ---------------------------------------------------------------------------

def data_cid_from_data_id(data_id: str) -> str:
    """
    Decimal CID (``data_cid``) of a ``0x...:0x...`` data_id: its second part.

    SerpApi's ``google_maps`` engine accepts ``data_cid`` on its own, so the
    restaurant's place details can be looked up from the data_id we already have.
    """
    return str(int(data_id.split(":")[1], 16))


def verify_place_identity(place: dict, data_id: str) -> tuple[bool, str | None]:
    """
    Confirm that a ``place_results`` object is the restaurant behind *data_id*.

    Returns ``(verified, field_used)``. The returned ``data_id`` is compared
    first, then ``data_cid``; a name match alone is never accepted.
    """
    returned_id = str(place.get("data_id") or "").strip().lower()
    if returned_id:
        return returned_id == data_id.strip().lower(), "data_id"
    returned_cid = str(place.get("data_cid") or "").strip()
    if returned_cid:
        return returned_cid == data_cid_from_data_id(data_id), "data_cid"
    return False, None


def photo_key(url: str) -> str:
    """
    Identity of a photo regardless of the requested size.

    Google image URLs end in a size suffix (``=w129-h86-k-no``), so the same
    photo appears under several URLs; stripping it lets duplicates be found.
    """
    if "googleusercontent.com" in url:
        return re.sub(r"=[^/=]*$", "", url)
    return url


def select_main_photo(place: dict) -> tuple[dict | None, str | None]:
    """
    Pick the restaurant's main photo from a ``place_results`` object.

    Uses the documented ``thumbnail`` field (the listing's primary image).
    Only when it is missing does it fall back to a listing category image
    (``images[]``, preferring the "All" category), labelled as a fallback.
    Returns ``(photo, None)`` or ``(None, reason)``.
    """
    thumb = place.get("thumbnail")
    if _is_http_url(thumb):
        return {
            "image_url": thumb.strip(),
            "image_type": IMAGE_TYPE_MAIN,
            "source_field": "place_results.thumbnail",
            "selection_method": "primary_listing_thumbnail",
            "is_fallback": False,
        }, None
    images = [i for i in place.get("images") or [] if isinstance(i, dict) and _is_http_url(i.get("thumbnail"))]
    pick = next((i for i in images if str(i.get("title") or "").strip().casefold() == "all"), None)
    pick = pick or (images[0] if images else None)
    if pick:
        return {
            "image_url": pick["thumbnail"].strip(),
            "image_type": IMAGE_TYPE_MAIN,
            "source_field": f"place_results.images[title={pick.get('title')!r}]",
            "selection_method": "fallback_listing_category_thumbnail",
            "is_fallback": True,
        }, None
    return None, "The place details response has no 'thumbnail' and no listing 'images'."


def extract_listing_photos(place: dict, exclude: set[str] | None = None) -> list[dict]:
    """
    Restaurant listing photos from ``place_results.images`` (one per category),
    skipping photos whose key is in *exclude* (e.g. the main photo) and duplicates.
    """
    seen = set(exclude or ())
    photos: list[dict] = []
    for img in place.get("images") or []:
        if not isinstance(img, dict) or not _is_http_url(img.get("thumbnail")):
            continue
        url = img["thumbnail"].strip()
        if photo_key(url) in seen:
            continue
        seen.add(photo_key(url))
        photos.append({"image_url": url, "image_type": IMAGE_TYPE_RESTAURANT,
                       "category": img.get("title") or None, "source_field": "place_results.images"})
    return photos


PLACE_REVIEWS_SOURCE = "place_results.user_reviews.most_relevant"
_REVIEW_ID_IN_LINK = re.compile(r"!1s([^!]+)!")


def extract_place_user_reviews(place: dict) -> list[dict]:
    """
    Customer reviews embedded in a ``place_results`` object
    (``user_reviews.most_relevant``), normalised like ``flatten_review``.

    These are Google's "most relevant" reviews and often carry the photos
    the reviewer attached (``images[].thumbnail``), which the newest reviews
    from the reviews engine may not have. The ``review_id`` is read from the
    review ``link`` so the same review from both sources can be merged.
    """
    reviews: list[dict] = []
    for r in (place.get("user_reviews") or {}).get("most_relevant") or []:
        if not isinstance(r, dict):
            continue
        match = _REVIEW_ID_IN_LINK.search(str(r.get("link") or ""))
        review = flatten_review({
            "review_id": match.group(1) if match else None,
            "user": {"name": r.get("username"), "reviews": r.get("user_review_count"),
                     "photos": r.get("user_photo_count")},
            "rating": r.get("rating"),
            "date": r.get("date"),
            "iso_date": r.get("date_iso8601"),
            "snippet": r.get("description"),
            "link": r.get("link"),
            "images": r.get("images"),
        })
        review["source"] = f"Google Maps place details ({PLACE_REVIEWS_SOURCE}) via SerpApi"
        for img in review["images"]:
            img["source_field"] = f"{PLACE_REVIEWS_SOURCE}[].images"
        reviews.append(review)
    return reviews


def extract_photos_api_photos(data: dict, exclude: set[str] | None = None) -> list[dict]:
    """Restaurant photos from one ``google_maps_photos`` response, deduplicated against *exclude*."""
    seen = set(exclude or ())
    photos: list[dict] = []
    for entry in data.get("photos") or []:
        if not isinstance(entry, dict):
            continue
        url = next((entry[k].strip() for k in _IMAGE_URL_KEYS if _is_http_url(entry.get(k))), None)
        if not url or photo_key(url) in seen:
            continue
        seen.add(photo_key(url))
        photos.append({"image_url": url, "image_type": IMAGE_TYPE_RESTAURANT,
                       "photo_id": entry.get("photo_id") or None, "source_field": "google_maps_photos.photos"})
    return photos


def describe_response(data: dict, http_status: int | None = None) -> dict:
    """
    Safe, credential-free summary of one SerpApi reviews response, used to
    debug what the API actually returned (structure only, no URLs/keys).
    """
    reviews = data.get("reviews") if isinstance(data.get("reviews"), list) else []
    pagination = data.get("serpapi_pagination")
    first = next((r for r in reviews if isinstance(r, dict)), None)
    first_images = first.get("images") if first else None
    image_entries = first_images if isinstance(first_images, list) else ([] if first_images is None else [first_images])
    shapes = sorted({
        "dict(" + ",".join(sorted(e)) + ")" if isinstance(e, dict) else type(e).__name__
        for e in image_entries
    })
    place = data.get("place_info") if isinstance(data.get("place_info"), dict) else {}
    search_info = data.get("search_information") if isinstance(data.get("search_information"), dict) else {}
    return {
        "http_status":             http_status,
        "search_status":           (data.get("search_metadata") or {}).get("status"),
        "api_error":               redact_secrets(str(data["error"])) if data.get("error") else None,
        "top_level_keys":          sorted(data),
        "place_title":             place.get("title"),
        "place_type":              place.get("type"),
        "reviews_results_state":   search_info.get("reviews_results_state"),
        "reviews_in_response":     len(reviews),
        "reviews_with_images":     sum(1 for r in reviews if isinstance(r, dict) and r.get("images")),
        "has_serpapi_pagination":  "serpapi_pagination" in data,
        "has_next_page_token":     isinstance(pagination, dict) and bool(pagination.get("next_page_token")),
        "first_review_keys":       sorted(first) if first else None,
        "first_review_has_images": bool(first) and "images" in first,
        "first_review_image_count": len(image_entries) if first else None,
        "first_review_image_entry_types": shapes if first else None,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_json(data: dict, filepath: Path | str) -> None:
    """
    Save *data* as an indented UTF-8 JSON file at *filepath*.

    Writes to a temporary file first and then replaces the target, so an
    interruption never leaves a half-written (corrupt) progress file.
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp = filepath.with_name(filepath.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)


def load_json(filepath: Path | str) -> dict:
    """Load a UTF-8 JSON file. Raises FileNotFoundError / ValueError."""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    with open(filepath, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cannot parse JSON from {filepath}: {exc}") from exc


def safe_name(value: str, max_len: int = 40) -> str:
    """Return a filesystem-safe fragment: only ``[A-Za-z0-9_-]`` characters."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "").strip("_")
    return cleaned[:max_len] or "unknown"


def safe_data_id(data_id: str, max_len: int = 40) -> str:
    """Filesystem-safe form of a Google Maps ``data_id`` (``:`` -> ``_``)."""
    return safe_name(data_id, max_len)


def review_filter_tag(query: str | None = None, topic_id: str | None = None) -> str | None:
    """
    Label for a filtered review search: ``query:<text>`` or ``topic:<id>``.

    Saved on every review the search returned (``matched_by``) and used to
    keep each filter's raw pages apart. None for an unfiltered search.
    """
    if topic_id:
        return f"topic:{topic_id.strip()}"
    if query:
        return f"query:{query.strip()}"
    return None


def filtered_dir(base: Path, review_filter: str | None) -> Path:
    """*base*, or its ``washroom/`` sub-folder for a filtered run."""
    return base / FILTERED_SUBDIR if review_filter else base


def _filter_slug(review_filter: str) -> str:
    return safe_name(review_filter.replace(":", "-"), 60)


def make_output_path(data_id: str, restaurant_name: str = "", review_filter: str | None = None) -> Path:
    """
    Return the stable per-restaurant output path under ``data/output/``.

    One file per restaurant: ``<restaurant_id>_reviews.json`` where the
    restaurant id is the filesystem-safe ``data_id``. Re-runs merge into this
    same file instead of creating new ones. *restaurant_name* is accepted for
    backwards compatibility and no longer affects the filename.

    Filtered runs (*review_filter* set) all merge into one file per
    restaurant in ``data/output/washroom/``.
    """
    folder = filtered_dir(OUTPUT_DIR, review_filter)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe_data_id(data_id)}_reviews.json"


def legacy_output_paths(data_id: str) -> list[Path]:
    """
    Output files written by earlier versions for *data_id*
    (``<name>_<data_id>.json``, ``restaurant_reviews_<data_id>[_<timestamp>].json``),
    newest first, so their reviews can be merged into the current file.
    """
    if not OUTPUT_DIR.exists():
        return []
    sid = safe_data_id(data_id)
    current = make_output_path(data_id)
    found = [p for p in OUTPUT_DIR.glob(f"*{sid}*.json") if p != current]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _page_stem(data_id: str, review_filter: str | None) -> str:
    sid = safe_data_id(data_id)
    return f"{sid}_{_filter_slug(review_filter)}" if review_filter else sid


def make_raw_page_path(data_id: str, page_num: int, review_filter: str | None = None) -> Path:
    """Path for a full (redacted) raw API response: ``data/raw_data/serpapi/`` (``.../washroom/`` when filtered)."""
    folder = filtered_dir(RAW_DATA_DIR / "serpapi", review_filter)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_page_stem(data_id, review_filter)}_page{page_num:03d}.json"


def make_extracted_page_path(data_id: str, page_num: int, review_filter: str | None = None) -> Path:
    """Path for a page-level extraction summary in ``data/extracted_pages/`` (``.../washroom/`` when filtered)."""
    folder = filtered_dir(EXTRACTED_PAGES_DIR, review_filter)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_page_stem(data_id, review_filter)}_page{page_num:03d}.json"


def make_cache_manifest_path(data_id: str, review_filter: str | None = None) -> Path:
    """Path of the manifest describing the latest cached raw pages for *data_id* (and filter)."""
    folder = filtered_dir(RAW_DATA_DIR / "serpapi", review_filter)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_page_stem(data_id, review_filter)}_cache.json"


def make_place_cache_path(data_id: str) -> Path:
    """Path of the cached (redacted) Google Maps place-details response for *data_id*."""
    folder = RAW_DATA_DIR / "serpapi"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe_data_id(data_id)}_place.json"


def make_photos_cache_path(data_id: str) -> Path:
    """Path of the cached (redacted) Google Maps Photos API pages for *data_id*."""
    folder = RAW_DATA_DIR / "serpapi"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe_data_id(data_id)}_photos.json"


def iter_cached_pages(data_id: str, review_filter: str | None = None) -> Iterator[dict]:
    """
    Yield the raw pages recorded by the latest live run for *data_id*
    (and *review_filter*), in order, so results can be rebuilt without
    spending API credits. Yields nothing if no cache manifest exists.
    """
    manifest_path = make_cache_manifest_path(data_id, review_filter)
    if not manifest_path.exists():
        return
    manifest = load_json(manifest_path)
    for rel in manifest.get("pages", []):
        yield load_json(PROJECT_ROOT / rel)


def extract_topics(data: dict) -> list[dict]:
    """The ``topics`` array of a first reviews page as ``{keyword, id, mentions}`` dicts."""
    return [
        {"keyword": t.get("keyword"), "id": t["id"], "mentions": t.get("mentions")}
        for t in data.get("topics") or []
        if isinstance(t, dict) and t.get("id")
    ]


def load_saved_topics(data_id: str) -> list[dict]:
    """
    Review topics for *data_id* read from saved first-page raw responses
    (unfiltered and filtered), deduplicated by topic id. Uses 0 credits.
    """
    sid = safe_data_id(data_id)
    raw_dir = RAW_DATA_DIR / "serpapi"
    paths = [raw_dir / f"{sid}_page001.json"]
    if (raw_dir / FILTERED_SUBDIR).exists():
        paths += sorted((raw_dir / FILTERED_SUBDIR).glob(f"{sid}_*_page001.json"))
    topics: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except (OSError, ValueError):
            continue
        for topic in extract_topics(data):
            if topic["id"] not in seen:
                seen.add(topic["id"])
                topics.append(topic)
    return topics


def short_hash(value: str, length: int = 10) -> str:
    """Deterministic short hex digest (used for unique, safe filenames)."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# HTTP / API layer
# ---------------------------------------------------------------------------

def _error_message(response: requests.Response) -> str:
    try:
        return str(response.json().get("error") or "")
    except ValueError:
        return ""


def fetch_page(
    api_key: str,
    params: dict,
    *,
    max_retries: int = MAX_RETRIES,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    meta: dict | None = None,
) -> dict:
    """
    Fetch a single page from SerpApi with retry and rate-limit handling.

    Only transient failures (timeouts, connection errors, HTTP 5xx and
    hourly rate limits) are retried; SerpApi does not bill failed searches.

    Args:
        api_key:     The SerpApi key (sent only in the request; never stored).
        params:      Query parameters (without ``api_key``).
        max_retries: Number of attempts before raising.
        timeout:     HTTP request timeout in seconds.
        meta:        Optional dict filled with ``http_status`` (and
                     ``api_message`` for "no results" bodies), also when
                     an exception is raised.

    Returns:
        Parsed JSON dict from the API response. A "no results" response is
        returned as a page with ``reviews == []``.

    Raises:
        InvalidApiKeyError, CreditsExhaustedError, SerpApiError.
        Error messages never contain the API key.
    """
    query = {k: v for k, v in params.items() if k != "api_key"}
    query["api_key"] = api_key

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(SERPAPI_ENDPOINT, params=query, timeout=timeout)
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                print(f"  [WARN] Request timed out. Retry {attempt}/{max_retries}...")
                time.sleep(5 * attempt)
                continue
            raise SerpApiError("Request repeatedly timed out. Check your network connection.")
        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                print(f"  [WARN] Connection error. Retry {attempt}/{max_retries}...")
                time.sleep(5 * attempt)
                continue
            raise SerpApiError("Cannot connect to SerpApi. Check your internet connection.")
        except requests.exceptions.RequestException as exc:
            # Do not echo str(exc): it can contain the full request URL.
            raise SerpApiError(f"HTTP request failed ({type(exc).__name__}).")

        status = response.status_code
        if meta is not None:
            meta["http_status"] = status
        error_msg = _error_message(response)
        lowered = error_msg.lower()

        if status == 401 or (status == 403 and "api key" in lowered):
            raise InvalidApiKeyError("Invalid API key (HTTP 401). Check your sources/.env file.")
        if status == 429:
            if any(kw in lowered for kw in ("run out of searches", "searches left", "plan", "credits")):
                raise CreditsExhaustedError(
                    "SerpApi credits exhausted: your plan has no searches left."
                )
            if attempt < max_retries:
                wait = 30 * attempt
                print(f"  [WARN] Rate limited (HTTP 429). Waiting {wait}s before retry {attempt}/{max_retries}...")
                time.sleep(wait)
                continue
            raise SerpApiError("Rate limited by SerpApi (HTTP 429). Try again later.")
        if status >= 500:
            if attempt < max_retries:
                wait = 10 * attempt
                print(f"  [WARN] Server error (HTTP {status}). Retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise SerpApiError(f"SerpApi server error (HTTP {status}).")
        if status == 400:
            detail = f": {error_msg}" if error_msg else ""
            raise SerpApiError(f"Bad request (HTTP 400){detail}. The data_id may be invalid or malformed.")
        if status != 200:
            detail = f": {error_msg}" if error_msg else ""
            raise SerpApiError(f"SerpApi returned HTTP {status}{detail}.")

        try:
            data = response.json()
        except ValueError:
            raise SerpApiError("SerpApi returned non-JSON content.")

        # SerpApi also reports some errors inside a 200 JSON body
        if data.get("error"):
            body_err = str(data["error"])
            low = body_err.lower()
            if any(kw in low for kw in _NO_RESULTS_KEYWORDS):
                if meta is not None:
                    meta["api_message"] = redact_secrets(body_err, api_key)
                data.setdefault("reviews", [])
                return data
            if "invalid api key" in low:
                raise InvalidApiKeyError("Invalid API key. Check your sources/.env file.")
            if any(kw in low for kw in _CREDIT_KEYWORDS):
                raise CreditsExhaustedError(
                    "SerpApi credit/quota exhausted. Your free-plan monthly limit may be reached."
                )
            raise SerpApiError(f"SerpApi API error: {redact_secrets(body_err, api_key)}")

        search_status = str((data.get("search_metadata") or {}).get("status") or "")
        if search_status.lower() == "error":
            raise SerpApiError("SerpApi reported search_metadata.status = 'Error' without details.")
        if "reviews" in data and not isinstance(data["reviews"], list):
            raise SerpApiError("Unexpected response: 'reviews' is not a list.")

        return data

    raise SerpApiError("All retries exhausted.")


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

_DATA_ID_RE = re.compile(r"^0x[0-9a-fA-F]+:0x[0-9a-fA-F]+$")


def is_valid_data_id(data_id: str) -> bool:
    """True if *data_id* looks like a Google Maps feature id (``0x...:0x...``)."""
    return bool(_DATA_ID_RE.match(data_id or ""))


def prompt_data_id(prompt: str = "Enter Google Maps restaurant data_id: ") -> str:
    """Prompt the user for a Google Maps data_id and validate it."""
    data_id = input(prompt).strip().strip('"').strip("'")
    if not data_id:
        print("[ERROR] data_id cannot be empty. Exiting.")
        sys.exit(1)
    if not is_valid_data_id(data_id):
        print("[ERROR] That does not look like a Google Maps data_id.")
        print("        Expected format: 0x3bae160000000000:0x123456789abcdef0")
        sys.exit(1)
    return data_id
