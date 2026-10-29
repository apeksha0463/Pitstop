"""
scripts/extract_reviews.py
==========================
Review collection core for Pitstop (also usable on its own).

Accepts a Google Maps restaurant ``data_id``, fetches reviews with SerpApi's
``google_maps_reviews`` engine (``sort_by=newestFirst``, ``hl=en``) using
``serpapi_pagination.next_page_token`` pagination, deduplicates them,
records the images attached to each review and saves one JSON file per
restaurant in ``data/output/`` after EVERY page.

Run on its own it records review image URLs without downloading them; use
``scripts/extract_review_images.py`` (or ``--download-images``) to also
download the images.

Files written:
  - data/output/<restaurant>_<data_id>.json      final / progress dataset
  - data/raw_data/serpapi/<data_id>_pageNNN.json full raw responses (API key removed)
  - data/raw_data/serpapi/<data_id>_cache.json   cache manifest for --from-cache
  - data/extracted_pages/<data_id>_pageNNN.json  page-level extracted reviews/images

Usage (from the repository root):

    python scripts/extract_reviews.py
    python scripts/extract_reviews.py --data-id 0xABCD:0x1234 --max-requests 3
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

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
        REVIEWS_PER_PAGE,
        SAVE_EVERY_N_PAGES,
        CreditsExhaustedError,
        InvalidApiKeyError,
        SerpApiError,
        ensure_dirs,
        extract_place_info,
        fetch_page,
        flatten_review,
        is_valid_data_id,
        iter_cached_pages,
        load_api_key,
        load_json,
        make_cache_manifest_path,
        make_extracted_page_path,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        prompt_data_id,
        redact_secrets,
        save_json,
        to_project_relative,
        validate_api_key,
    )
    from scripts.extract_review_images import ReviewImageDownloader, STATUS_ALREADY, STATUS_DOWNLOADED
except ImportError:
    from utils import (
        DIVIDER,
        MAX_REQUESTS,
        REQUEST_DELAY_SECONDS,
        REVIEWS_PER_PAGE,
        SAVE_EVERY_N_PAGES,
        CreditsExhaustedError,
        InvalidApiKeyError,
        SerpApiError,
        ensure_dirs,
        extract_place_info,
        fetch_page,
        flatten_review,
        is_valid_data_id,
        iter_cached_pages,
        load_api_key,
        load_json,
        make_cache_manifest_path,
        make_extracted_page_path,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        prompt_data_id,
        redact_secrets,
        save_json,
        to_project_relative,
        validate_api_key,
    )
    from extract_review_images import ReviewImageDownloader, STATUS_ALREADY, STATUS_DOWNLOADED

SORT_BY: str = "newestFirst"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def print_banner() -> None:
    print(f"\n{DIVIDER}")
    print("   GOOGLE MAPS REVIEW EXTRACTOR")
    print("   Powered by SerpApi")
    print(f"{DIVIDER}\n")


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def _image_stats(reviews: list[dict]) -> dict:
    images = [img for r in reviews for img in r.get("images") or []]
    unique_urls = {img.get("image_url") for img in images if img.get("image_url")}
    available = [img for img in images if img.get("download_status") in (STATUS_DOWNLOADED, STATUS_ALREADY)]
    unique_files = {img.get("local_path") for img in available if img.get("local_path")}
    return {
        "reviews_with_images":  sum(1 for r in reviews if r.get("images")),
        "images_found":         len(images),
        "unique_image_urls":    len(unique_urls),
        "images_downloaded":    len(available),
        "unique_image_files":   len(unique_files),
        "images_failed":        sum(
            1 for img in images
            if img.get("download_status") not in (STATUS_DOWNLOADED, STATUS_ALREADY, "not_requested")
        ),
    }


def build_output(
    *,
    data_id: str,
    place_info: dict,
    reviews: list[dict],
    collection_status: str,
    pages_fetched: int,
    requests_used: int,
    max_requests: int,
    duplicates_skipped: int,
    previous_retained: int,
    images_folder: Optional[str],
    from_cache: bool,
    stop_reason: Optional[str],
) -> dict:
    """Assemble the per-restaurant output document (never contains the API key)."""
    return {
        "source":                    "SerpApi Google Maps Reviews",
        "data_id":                   data_id,
        "restaurant_name":           place_info.get("restaurant_name", "Unknown"),
        "address":                   place_info.get("address"),
        "rating":                    place_info.get("rating"),
        "type":                      place_info.get("type"),
        "phone":                     place_info.get("phone"),
        "website":                   place_info.get("website"),
        "total_reviews_reported":    place_info.get("total_reviews_reported"),
        "reviews_collected":         len(reviews),
        **_image_stats(reviews),
        "duplicates_skipped":        duplicates_skipped,
        "previous_reviews_retained": previous_retained,
        "pages_fetched":             pages_fetched,
        "api_requests_used":         requests_used,
        "max_requests":              max_requests,
        "sort_by":                   SORT_BY,
        "from_cache":                from_cache,
        "collection_complete":       collection_status == "completed",
        "collection_status":         collection_status,
        "stop_reason":               stop_reason,
        "images_folder":             images_folder,
        "extracted_at":              datetime.datetime.now().isoformat(timespec="seconds"),
        "reviews":                   reviews,
    }


def _load_previous_reviews(output_path: Path, data_id: str) -> list[dict]:
    """Reviews saved by an earlier run for the same restaurant (merged, never lost)."""
    if not output_path.exists():
        return []
    try:
        previous = load_json(output_path)
    except (OSError, ValueError):
        print(f"  [WARN] Existing {output_path.name} is unreadable; it will be replaced.")
        return []
    if previous.get("data_id") != data_id:
        return []
    return [r for r in previous.get("reviews") or [] if isinstance(r, dict)]


def _merge_images(target: dict, extra: dict) -> None:
    """Add image URLs from a duplicate copy of a review that the first copy lacked."""
    known = {img.get("image_url") for img in target.get("images") or []}
    for img in extra.get("images") or []:
        if img.get("image_url") not in known:
            target.setdefault("images", []).append({**img, "image_index": len(target["images"]) + 1})
            known.add(img.get("image_url"))
    target["images_count"] = len(target.get("images") or [])


# ---------------------------------------------------------------------------
# Core extraction loop
# ---------------------------------------------------------------------------

def run_extraction(
    api_key: Optional[str],
    data_id: str,
    output_path: Optional[Path] = None,
    *,
    max_requests: int = MAX_REQUESTS,
    delay: float = REQUEST_DELAY_SECONDS,
    save_every: int = SAVE_EVERY_N_PAGES,
    save_raw_pages: bool = True,
    downloader: Optional[ReviewImageDownloader] = None,
    cached_pages: Optional[Iterable[dict]] = None,
) -> Optional[dict]:
    """
    Fetch reviews (and their attached images) and save them.

    Args:
        api_key:        SerpApi key (unused when *cached_pages* is given).
        data_id:        Google Maps restaurant ``data_id``.
        output_path:    Destination JSON; defaults to ``make_output_path``
                        once the restaurant name is known.
        max_requests:   Maximum SerpApi search requests (free-plan guard).
        delay:          Seconds to sleep between API requests.
        save_every:     Save progress every N pages (1 = every page).
        save_raw_pages: Save raw/page-level responses under ``data/``.
        downloader:     Image downloader; if None, image URLs are recorded
                        with ``download_status = "not_requested"``.
        cached_pages:   Previously saved raw pages to process instead of
                        calling the API (0 credits used).

    Returns:
        The output dict, or None if nothing could be collected.
    """
    from_cache = cached_pages is not None
    page_source = iter(cached_pages) if from_cache else None

    print(f"Restaurant data_id : {data_id}")
    if from_cache:
        print("Mode               : cached raw responses (0 API credits)")
    else:
        print(f"Max API requests   : {max_requests}")
    print()

    run_reviews: dict[str, dict] = {}
    previous_reviews: list[dict] = []
    duplicates_skipped = 0
    pages_fetched = 0
    requests_used = 0
    collection_status = "in_progress"
    stop_reason: Optional[str] = None
    place_info: dict = {}
    images_folder = to_project_relative(downloader.folder) if downloader else None
    cache_pages: list[str] = []
    last_next_token: Optional[str] = None

    base_params = {
        "engine":  "google_maps_reviews",
        "data_id": data_id,
        "hl":      "en",
        "sort_by": SORT_BY,
    }
    params = dict(base_params)

    def assemble(status: str) -> dict:
        fps = set(run_reviews)
        retained = [r for r in previous_reviews if make_review_fingerprint(r) not in fps]
        return build_output(
            data_id=data_id, place_info=place_info,
            reviews=list(run_reviews.values()) + retained,
            collection_status=status, pages_fetched=pages_fetched,
            requests_used=requests_used, max_requests=max_requests,
            duplicates_skipped=duplicates_skipped, previous_retained=len(retained),
            images_folder=images_folder, from_cache=from_cache, stop_reason=stop_reason,
        )

    def save_cache_manifest(complete: bool) -> None:
        if from_cache or not save_raw_pages:
            return
        save_json(
            {
                "data_id": data_id, "sort_by": SORT_BY,
                "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "pagination_complete": complete, "last_next_page_token": last_next_token,
                "pages": cache_pages,
            },
            make_cache_manifest_path(data_id),
        )

    try:
        page_num = 0
        while True:
            page_num += 1
            if from_cache:
                data = next(page_source, None)
                if data is None:
                    collection_status = "partial_from_cache" if last_next_token else "completed"
                    if last_next_token:
                        stop_reason = "Cached pages end before pagination finished."
                    break
                print(f"Loading cached review page {page_num}...")
            else:
                if requests_used >= max_requests:
                    collection_status = "stopped_at_request_limit"
                    stop_reason = (f"Reached the request budget of {max_requests}. "
                                   "Run again with a larger --max-requests to continue.")
                    print(f"\n  -> {stop_reason}")
                    break
                if requests_used > 0 and delay > 0:
                    time.sleep(delay)
                print(f"Fetching review page {page_num}...")
                try:
                    requests_used += 1
                    data = fetch_page(api_key, params)
                except InvalidApiKeyError as exc:
                    collection_status, stop_reason = "stopped_invalid_api_key", str(exc)
                except CreditsExhaustedError as exc:
                    collection_status, stop_reason = "stopped_credits_exhausted", str(exc)
                except (SerpApiError, RuntimeError) as exc:
                    collection_status, stop_reason = "stopped_on_error", str(exc) or "API request failed."
                if stop_reason:
                    print(f"\n[ERROR] {stop_reason}")
                    if pages_fetched == 0:
                        print("No reviews were collected. Nothing was saved.")
                        return None
                    break

            pages_fetched += 1

            if save_raw_pages and not from_cache:
                raw_path = make_raw_page_path(data_id, pages_fetched)
                save_json(redact_secrets(data, api_key), raw_path)
                cache_pages.append(to_project_relative(raw_path))

            if pages_fetched == 1:
                place_info = extract_place_info(data)
                if output_path is None:
                    output_path = make_output_path(data_id, place_info["restaurant_name"])
                previous_reviews = _load_previous_reviews(output_path, data_id)
                print(f"\nRestaurant : {place_info['restaurant_name']}")
                if place_info.get("address"):
                    print(f"Address    : {place_info['address']}")
                if place_info.get("total_reviews_reported"):
                    print(f"Total reviews on Google Maps : {place_info['total_reviews_reported']}")
                if previous_reviews:
                    print(f"Previously saved reviews     : {len(previous_reviews)} (kept and merged)")
                print()

            raw_reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict)]
            page_new: list[dict] = []
            for raw in raw_reviews:
                review = flatten_review(raw)
                fp = make_review_fingerprint(raw)
                if fp in run_reviews:
                    duplicates_skipped += 1
                    _merge_images(run_reviews[fp], review)
                    target = run_reviews[fp]
                else:
                    run_reviews[fp] = review
                    page_new.append(review)
                    target = review
                if downloader is not None:
                    downloader.process_review(target)
                else:
                    for img in target.get("images") or []:
                        img.setdefault("local_path", None)
                        img.setdefault("download_status", "not_requested")

            pagination = data.get("serpapi_pagination") or {}
            last_next_token = pagination.get("next_page_token") or None

            if save_raw_pages and not from_cache:
                save_json(
                    {"data_id": data_id, "page": pages_fetched,
                     "has_next_page": bool(last_next_token),
                     "reviews_on_page": len(raw_reviews), "new_reviews": len(page_new),
                     "reviews": [{"review_id": r["review_id"], "rating": r["rating"], "date": r["date"],
                                  "images": r["images"]} for r in page_new]},
                    make_extracted_page_path(data_id, pages_fetched),
                )

            stats = _image_stats(list(run_reviews.values()))
            print(f"Reviews collected: {len(run_reviews)}")
            print(f"Reviews with images: {stats['reviews_with_images']}")
            print(f"Review images found: {stats['images_found']}")
            print(f"Images downloaded: {stats['images_downloaded']}")
            print()

            if not last_next_token:
                collection_status = "completed"
                print("  -> No next-page token: all available pages retrieved.")
                break
            if not raw_reviews:
                collection_status = "stopped_empty_page"
                stop_reason = "SerpApi returned an empty page that still had a next-page token."
                break

            params = {**base_params, "next_page_token": last_next_token, "num": REVIEWS_PER_PAGE}

            if save_every > 0 and pages_fetched % save_every == 0:
                save_json(assemble("in_progress"), output_path)
                save_cache_manifest(False)

    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] User cancelled. Saving collected reviews...")
        collection_status = "interrupted_by_user"
        stop_reason = "Interrupted by user."
        if output_path is None:
            return None

    # -----------------------------------------------------------------------
    # Final save
    # -----------------------------------------------------------------------
    output = assemble(collection_status)
    save_json(output, output_path)
    save_cache_manifest(collection_status == "completed")

    print(f"\n{DIVIDER}")
    print("  EXTRACTION SUMMARY")
    print(f"{DIVIDER}")
    print(f"  Restaurant          : {output['restaurant_name']}")
    print(f"  Reviews collected   : {output['reviews_collected']}"
          + (f" (of {output['total_reviews_reported']} reported)" if output["total_reviews_reported"] else ""))
    print(f"  Reviews with images : {output['reviews_with_images']}")
    print(f"  Review images found : {output['images_found']}")
    print(f"  Images downloaded   : {output['images_downloaded']}")
    if output["images_failed"]:
        print(f"  Images not saved    : {output['images_failed']} (see download_status in JSON)")
    print(f"  API requests used   : {requests_used}")
    print(f"{DIVIDER}")
    print(f"Final JSON: {to_project_relative(output_path)}")
    if images_folder:
        print(f"Images folder: {images_folder}/")
    print(f"Collection status: {collection_status}")
    if stop_reason and collection_status != "completed":
        print(f"Reason: {stop_reason}")
    if collection_status != "completed":
        print("NOTE: Collection is INCOMPLETE — more reviews may exist.")
    print()
    return output


# ---------------------------------------------------------------------------
# Shared CLI flow (also used by extract_review_images.py)
# ---------------------------------------------------------------------------

def run_cli(
    data_id: str,
    *,
    max_requests: int = MAX_REQUESTS,
    from_cache: bool = False,
    skip_key_check: bool = False,
    downloader: Optional[ReviewImageDownloader] = None,
    save_raw_pages: bool = True,
) -> Optional[dict]:
    """Load/validate the key (or the cache), then run the extraction."""
    ensure_dirs()

    if from_cache:
        try:
            pages = list(iter_cached_pages(data_id))
        except (OSError, ValueError) as exc:
            print(f"[ERROR] Could not read cached pages: {exc}")
            return None
        if not pages:
            print("[ERROR] No cached responses found for this data_id. Run once without --from-cache.")
            return None
        return run_extraction(None, data_id, max_requests=0, downloader=downloader,
                              cached_pages=pages, save_raw_pages=save_raw_pages)

    api_key = load_api_key()
    if not skip_key_check:
        print("Checking SerpApi key (account check, uses no search credits)...")
        try:
            account = validate_api_key(api_key)
        except (InvalidApiKeyError, CreditsExhaustedError) as exc:
            print(f"[ERROR] {exc}")
            return None
        if account is None:
            print("  [WARN] Could not verify the key right now; continuing.")
        else:
            left = account.get("total_searches_left")
            print(f"  Key OK. Plan: {account.get('plan_name') or 'unknown'}"
                  + (f" | searches left: {left}" if left is not None else ""))
            if isinstance(left, int) and left < max_requests:
                print(f"  Request budget reduced from {max_requests} to {left} (remaining credits).")
                max_requests = left
        print()

    return run_extraction(api_key, data_id, max_requests=max_requests,
                          downloader=downloader, save_raw_pages=save_raw_pages)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Google Maps reviews for a restaurant using SerpApi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python scripts/extract_reviews.py\n"
            "  python scripts/extract_reviews.py --data-id 0x3bae160000000000:0x123456789abcdef0\n"
        ),
    )
    parser.add_argument("--data-id", metavar="DATA_ID", default=None,
                        help="Google Maps restaurant data_id (prompted interactively if omitted).")
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS,
                        help=f"Maximum number of SerpApi requests (default: {MAX_REQUESTS}).")
    parser.add_argument("--download-images", action="store_true",
                        help="Also download review images (same as extract_review_images.py).")
    parser.add_argument("--from-cache", action="store_true",
                        help="Rebuild from the last saved raw responses (0 API credits).")
    parser.add_argument("--no-raw-pages", action="store_true", default=False,
                        help="Skip saving raw responses to data/raw_data/ and data/extracted_pages/.")
    parser.add_argument("--skip-key-check", action="store_true",
                        help="Skip the free SerpApi account check before searching.")
    args = parser.parse_args(argv)
    if args.max_requests < 1:
        parser.error("--max-requests must be at least 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    print_banner()

    data_id = args.data_id or prompt_data_id()
    if not is_valid_data_id(data_id):
        print("[ERROR] Invalid data_id format. Expected 0x...:0x...")
        return 1

    result = run_cli(
        data_id,
        max_requests=args.max_requests,
        from_cache=args.from_cache,
        skip_key_check=args.skip_key_check,
        downloader=ReviewImageDownloader(data_id, enabled=args.download_images),
        save_raw_pages=not args.no_raw_pages,
    )
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
