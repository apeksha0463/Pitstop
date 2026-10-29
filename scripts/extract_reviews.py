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
  - data/output/<data_id>_reviews.json           final / progress dataset
  - data/raw_data/serpapi/<data_id>_pageNNN.json full raw responses (API key removed)
  - data/raw_data/serpapi/<data_id>_cache.json   cache manifest for --from-cache
  - data/raw_data/serpapi/<data_id>_place.json   cached place details (main restaurant photo)
  - data/extracted_pages/<data_id>_pageNNN.json  page-level extracted reviews/images

Usage (from the repository root):

    python scripts/extract_reviews.py
    python scripts/extract_reviews.py --data-id 0xABCD:0x1234 --max-requests 3
    python scripts/extract_reviews.py --data-id 0xABCD:0x1234 --query washroom

With ``--query`` or ``--topic-id`` only matching reviews are fetched; each is
tagged in ``matched_by`` and saved to data/output/washroom/<data_id>_reviews.json
(raw pages under data/raw_data/serpapi/washroom/).
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
        describe_response,
        ensure_dirs,
        extract_place_info,
        extract_topics,
        fetch_page,
        flatten_review,
        is_valid_data_id,
        iter_cached_pages,
        legacy_output_paths,
        load_api_key,
        load_json,
        make_cache_manifest_path,
        make_extracted_page_path,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        name_matches,
        photo_key,
        prompt_data_id,
        redact_secrets,
        review_filter_tag,
        save_json,
        to_project_relative,
        validate_api_key,
    )
    from scripts.extract_review_images import ReviewImageDownloader, STATUS_ALREADY, STATUS_DOWNLOADED
    from scripts.restaurant_photos import build_image_gallery, collect_restaurant_photos
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
        describe_response,
        ensure_dirs,
        extract_place_info,
        extract_topics,
        fetch_page,
        flatten_review,
        is_valid_data_id,
        iter_cached_pages,
        legacy_output_paths,
        load_api_key,
        load_json,
        make_cache_manifest_path,
        make_extracted_page_path,
        make_output_path,
        make_raw_page_path,
        make_review_fingerprint,
        name_matches,
        photo_key,
        prompt_data_id,
        redact_secrets,
        review_filter_tag,
        save_json,
        to_project_relative,
        validate_api_key,
    )
    from extract_review_images import ReviewImageDownloader, STATUS_ALREADY, STATUS_DOWNLOADED
    from restaurant_photos import build_image_gallery, collect_restaurant_photos

SORT_BY: str = "newestFirst"
# Documented sort_by values (https://serpapi.com/google-maps-reviews-api)
SORT_CHOICES: tuple[str, ...] = ("qualityScore", "newestFirst", "ratingHigh", "ratingLow")

# collection_status values
STATUS_COMPLETED = "completed"                    # pagination finished
STATUS_NO_REVIEWS = "completed_with_no_reviews"   # API confirmed there are no reviews
STATUS_API_ERROR = "api_error"                    # request/response failed, nothing usable
STATUS_QUOTA = "quota_exhausted"                  # account has no usable credits
STATUS_PARTIAL = "partial"                        # some results, could not finish
STATUS_REQUEST_LIMIT = "stopped_at_request_limit" # configured request budget reached
SUCCESS_STATUSES = frozenset({STATUS_COMPLETED, STATUS_NO_REVIEWS})
FAILURE_STATUSES = frozenset({STATUS_API_ERROR, STATUS_QUOTA})


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
    failed = sum(
        1 for img in images
        if img.get("download_status") not in (STATUS_DOWNLOADED, STATUS_ALREADY, "not_requested")
    )
    return {
        "reviews_with_images":    sum(1 for r in reviews if r.get("images")),
        "images_found":           len(images),
        "unique_image_urls":      len(unique_urls),
        "images_downloaded":      len(available),
        "unique_image_files":     len(unique_files),
        "images_failed":          failed,
        "images_download_failed": failed,
    }


def build_image_summary(main_photo: Optional[dict], reviews: list[dict]) -> dict:
    """Image counts for the output JSON. The main listing photo is never counted as a review image."""
    stats = _image_stats(reviews)
    return {
        "main_photo_downloaded":          bool(main_photo) and main_photo.get("download_status") in (STATUS_DOWNLOADED, STATUS_ALREADY),
        "main_photo_is_fallback":         bool(main_photo and main_photo.get("is_fallback")),
        "reviews_collected":              len(reviews),
        "reviews_with_images":            stats["reviews_with_images"],
        "review_images_found":            stats["images_found"],
        "unique_review_image_urls":       stats["unique_image_urls"],
        "review_images_downloaded":       stats["images_downloaded"],
        "unique_review_image_files":      stats["unique_image_files"],
        "review_image_download_failures": stats["images_failed"],
    }


def review_coverage(place_info: dict, reviews_collected: int, collection_status: str,
                    page_diagnostics: list[dict]) -> dict:
    """
    How much of the place's review history the API actually returned.

    Pagination ending (no ``next_page_token``) only means SerpApi offered no
    more pages; it does not prove every review Google reports was collected.
    """
    reported = place_info.get("total_reviews_reported")
    complete = isinstance(reported, int) and reviews_collected >= reported
    note = None
    if isinstance(reported, int) and not complete:
        if collection_status in SUCCESS_STATUSES:
            first = page_diagnostics[0] if page_diagnostics else {}
            missing = "" if first.get("has_serpapi_pagination", True) else (
                " The first response had no 'serpapi_pagination' block at all.")
            note = (f"SerpApi stopped offering pages after {reviews_collected} of the {reported} reviews "
                    f"Google reports for this place.{missing} Only the reviews returned by the API were "
                    "collected; run again with --fresh to bypass SerpApi's cached result.")
        else:
            note = (f"{reviews_collected} of the {reported} reviews Google reports were collected; "
                    "pagination did not finish.")
    return {
        "reviews_reported_by_google":    reported,
        "reviews_collected":             reviews_collected,
        "all_reported_reviews_collected": complete if isinstance(reported, int) else None,
        "note":                          note,
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
    api_error: Optional[dict] = None,
    page_diagnostics: Optional[list[dict]] = None,
    sort_by: str = SORT_BY,
    expected_name: Optional[str] = None,
    restaurant_media: Optional[dict] = None,
    review_filter: Optional[str] = None,
    topics: Optional[list[dict]] = None,
    filter_runs: Optional[list[dict]] = None,
    next_page_token: Optional[str] = None,
) -> dict:
    """
    Assemble the per-restaurant output document (never contains the API key).

    The document leads with ``restaurant_name``, ``data_id``,
    ``main_restaurant_photo``, ``image_gallery`` (main listing photo ->
    customer review photos -> other listing photos), ``reviews`` and
    ``image_summary``; collection metadata follows.

    Filtered runs (``--query`` / ``--topic-id``) also record the filter,
    the page-1 review ``topics``, the history of filtered runs
    (``filter_runs``) and the ``next_page_token`` to resume from.
    """
    name = place_info.get("restaurant_name", "Unknown")
    media = restaurant_media or {}
    main_photo = media.get("main_restaurant_photo")
    restaurant_photos = media.get("restaurant_photos") or []
    lookup = media.get("place_lookup")
    if lookup:
        lookup = {**lookup, "title_matches_reviews_place":
                  name_matches(lookup.get("returned_title"), name if place_info else None)}
    unavailable = None if main_photo else (
        media.get("main_restaurant_photo_unavailable_reason") or "The restaurant details lookup was not run.")
    media_requests = media.get("api_requests_used") or 0
    filtered = {} if not review_filter else {
        "review_filter":   review_filter,
        "topics":          topics or [],
        "filter_runs":     filter_runs or [],
        "next_page_token": next_page_token,
    }
    return {
        "restaurant_name":           name,
        "data_id":                   data_id,
        "main_restaurant_photo":     main_photo,
        "image_gallery":             build_image_gallery(main_photo, restaurant_photos, reviews),
        "reviews":                   reviews,
        "image_summary":             build_image_summary(main_photo, reviews),
        "review_coverage":           review_coverage(place_info, len(reviews), collection_status,
                                                     page_diagnostics or []),
        "source":                    "SerpApi Google Maps Reviews",
        "restaurant_name_source":    place_info.get("name_source"),
        "expected_restaurant_name":  expected_name,
        "restaurant_name_matches_expected": name_matches(name if place_info else None, expected_name),
        "place_warning":             place_info.get("place_warning"),
        "address":                   place_info.get("address"),
        "rating":                    place_info.get("rating"),
        "type":                      place_info.get("type"),
        "phone":                     place_info.get("phone"),
        "website":                   place_info.get("website"),
        "total_reviews_reported":    place_info.get("total_reviews_reported"),
        "reviews_results_state":     place_info.get("reviews_results_state"),
        "main_restaurant_photo_unavailable_reason": unavailable,
        "restaurant_photos":         restaurant_photos,
        "place_lookup":              lookup,
        "reviews_collected":         len(reviews),
        **_image_stats(reviews),
        "duplicates_skipped":        duplicates_skipped,
        "previous_reviews_retained": previous_retained,
        "pages_fetched":             pages_fetched,
        "api_requests_used":         requests_used,
        "restaurant_details_requests_used": media_requests,
        "api_requests_used_total":   requests_used + media_requests,
        "max_requests":              max_requests,
        "sort_by":                   sort_by,
        "from_cache":                from_cache,
        "collection_complete":       collection_status in SUCCESS_STATUSES,
        "collection_status":         collection_status,
        "stop_reason":               stop_reason,
        "api_error":                 api_error,
        "page_diagnostics":          page_diagnostics or [],
        "images_folder":             images_folder,
        "extracted_at":              datetime.datetime.now().isoformat(timespec="seconds"),
        **filtered,
    }


def _load_previous_outputs(output_path: Path, data_id: str, include_legacy: bool = True) -> list[dict]:
    """
    Documents saved by earlier runs for *data_id*: the current output file,
    or (before the first run with the current naming) the files written
    under older names, newest first. Filtered runs pass
    ``include_legacy=False`` so the unfiltered history is never merged in.
    """
    if output_path.exists():
        paths = [output_path]
    else:
        paths = legacy_output_paths(data_id) if include_legacy else []
    docs: list[dict] = []
    for path in paths:
        try:
            doc = load_json(path)
        except (OSError, ValueError):
            print(f"  [WARN] Existing {path.name} is unreadable; it will not be merged.")
            continue
        if doc.get("data_id") == data_id:
            docs.append(doc)
    return docs


def _previous_reviews(docs: list[dict]) -> list[dict]:
    """Reviews saved by earlier runs for the same restaurant (merged, never lost)."""
    seen: set[str] = set()
    reviews: list[dict] = []
    for doc in docs:
        for r in doc.get("reviews") or []:
            if isinstance(r, dict) and make_review_fingerprint(r) not in seen:
                seen.add(make_review_fingerprint(r))
                reviews.append(r)
    return reviews


_MEDIA_KEYS = ("main_restaurant_photo", "main_restaurant_photo_unavailable_reason", "restaurant_photos", "place_lookup")


def _previous_media(docs: list[dict]) -> Optional[dict]:
    """Restaurant photos saved by an earlier run, kept when this run did not look them up."""
    doc = next((d for d in docs if d.get("place_lookup")), None)
    return {k: doc.get(k) for k in _MEDIA_KEYS} if doc else None


def _previous_topics(docs: list[dict]) -> list[dict]:
    return next((d["topics"] for d in docs if d.get("topics")), [])


def _merge_topics(old: list[dict], new: list[dict]) -> list[dict]:
    """Union of two topic lists by ``id``; the newer counts win."""
    merged = {t["id"]: t for t in old if isinstance(t, dict) and t.get("id")}
    merged.update({t["id"]: t for t in new})
    return list(merged.values())


def _tag_review(review: dict, tags: Iterable[str]) -> None:
    """Add filter tags to ``review['matched_by']`` (kept unique, in order)."""
    matched = review.setdefault("matched_by", [])
    for tag in tags:
        if tag and tag not in matched:
            matched.append(tag)


def _merge_images(target: dict, extra: dict) -> None:
    """Add image URLs from a duplicate copy of a review that the first copy lacked."""
    # Compare by photo identity: the same photo can arrive under another size suffix
    known = {photo_key(img.get("image_url") or "") for img in target.get("images") or []}
    for img in extra.get("images") or []:
        key = photo_key(img.get("image_url") or "")
        if key not in known:
            target.setdefault("images", []).append({**img, "image_index": len(target["images"]) + 1})
            known.add(key)
    target["images_count"] = len(target.get("images") or [])


# ---------------------------------------------------------------------------
# Core extraction loop
# ---------------------------------------------------------------------------

def _print_diagnostics(page_num: int, diag: dict) -> None:
    print(f"  [DIAG] page {page_num}: HTTP {diag['http_status'] if diag['http_status'] is not None else 'n/a (cache)'}"
          f" | search_status={diag['search_status']} | api_error={diag['api_error'] or 'none'}")
    print(f"  [DIAG]   top-level keys     : {', '.join(diag['top_level_keys'])}")
    print(f"  [DIAG]   place              : {diag['place_title']!r} (type: {diag['place_type']!r})")
    if diag["reviews_results_state"]:
        print(f"  [DIAG]   reviews_results_state: {diag['reviews_results_state']!r}")
    print(f"  [DIAG]   reviews in response: {diag['reviews_in_response']} "
          f"({diag['reviews_with_images']} with images)")
    print(f"  [DIAG]   serpapi_pagination : {diag['has_serpapi_pagination']} | "
          f"next_page_token: {diag['has_next_page_token']}")
    if diag["first_review_keys"] is not None:
        print(f"  [DIAG]   first review keys  : {', '.join(diag['first_review_keys'])}")
        print(f"  [DIAG]   first review images: field present={diag['first_review_has_images']}, "
              f"count={diag['first_review_image_count']}, entry types={diag['first_review_image_entry_types']}")


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
    sort_by: str = SORT_BY,
    diagnostics: bool = False,
    expected_name: Optional[str] = None,
    restaurant_media: Optional[dict] = None,
    fresh: bool = False,
    query: Optional[str] = None,
    topic_id: Optional[str] = None,
    resume_token: Optional[str] = None,
    first_page: int = 1,
) -> dict:
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
        sort_by:        SerpApi ``sort_by`` value (see ``SORT_CHOICES``).
        diagnostics:    Print a safe structural summary of every response.
        expected_name:  Optional restaurant name to verify against the
                        name returned by the API (never substituted).
        restaurant_media: Result of ``collect_restaurant_photos`` (main and
                        other restaurant photos). When None, those saved by
                        an earlier run are kept.
        fresh:          Send SerpApi's documented ``no_cache=true`` so a
                        cached (possibly truncated) result is not reused.
        query:          SerpApi ``query``: only reviews matching this text.
        topic_id:       SerpApi ``topic_id``: only reviews about this topic
                        (mutually exclusive with *query*). With either
                        filter, page 1 also asks for ``num=20``, reviews are
                        tagged in ``matched_by`` and everything is saved
                        under the ``washroom/`` sub-folders.
        resume_token:   Continue a filtered search from this saved
                        ``next_page_token`` instead of page 1.
        first_page:     Page number of the first page fetched (for raw
                        page filenames when resuming).

    Returns:
        The output dict, always saved to disk. ``collection_status`` tells
        whether it is complete (see the ``STATUS_*`` constants); API
        failures are saved with ``api_error`` details instead of being
        reported as a completed, empty extraction.
    """
    if query and topic_id:
        raise ValueError("query and topic_id can't be used together (SerpApi).")
    review_filter = review_filter_tag(query, topic_id)
    from_cache = cached_pages is not None
    page_source = iter(cached_pages) if from_cache else None

    print(f"Restaurant data_id : {data_id}")
    print(f"Sort order         : {sort_by}")
    if review_filter:
        print(f"Review filter      : {review_filter}")
    if from_cache:
        print("Mode               : cached raw responses (0 API credits)")
    else:
        print(f"Max API requests   : {max_requests}")
    print()

    if output_path is None:
        output_path = make_output_path(data_id, review_filter=review_filter)
    previous_docs = _load_previous_outputs(output_path, data_id, include_legacy=not review_filter)
    previous_reviews = _previous_reviews(previous_docs)
    previous_media = _previous_media(previous_docs)

    run_reviews: dict[str, dict] = {}
    duplicates_skipped = 0
    pages_fetched = 0
    requests_used = 0
    collection_status = "in_progress"
    stop_reason: Optional[str] = None
    api_error: Optional[dict] = None
    page_diagnostics: list[dict] = []
    place_info: dict = {}
    images_folder = to_project_relative(downloader.folder) if downloader else None
    cache_pages: list[str] = []
    # A resumed run that fails before its first page still records where to continue
    last_next_token: Optional[str] = resume_token
    topics: list[dict] = []

    base_params = {
        "engine":  "google_maps_reviews",
        "data_id": data_id,
        "hl":      "en",
        "sort_by": sort_by,
    }
    if fresh:
        base_params["no_cache"] = "true"
    if query:
        base_params["query"] = query
    if topic_id:
        base_params["topic_id"] = topic_id
    params = dict(base_params)
    if review_filter:
        # SerpApi allows num on the first page once query/topic_id is set
        params["num"] = REVIEWS_PER_PAGE
    if resume_token:
        params = {**base_params, "next_page_token": resume_token, "num": REVIEWS_PER_PAGE}

    def assemble(status: str) -> dict:
        fps = set(run_reviews)
        retained = [r for r in previous_reviews if make_review_fingerprint(r) not in fps]
        for prev in previous_reviews:
            fp = make_review_fingerprint(prev)
            if fp in run_reviews:
                # Keep what earlier filters matched this review with
                _tag_review(run_reviews[fp], prev.get("matched_by") or [])
        run_record = {
            "review_filter":     review_filter,
            "first_page":        first_page,
            "pages_fetched":     pages_fetched,
            "requests_used":     requests_used,
            "reviews_returned":  len(run_reviews),
            "collection_status": status,
            "next_page_token":   last_next_token,
            "from_cache":        from_cache,
            "run_at":            datetime.datetime.now().isoformat(timespec="seconds"),
        }
        previous_runs = (previous_docs[0].get("filter_runs") or []) if previous_docs else []
        return build_output(
            data_id=data_id, place_info=place_info,
            reviews=list(run_reviews.values()) + retained,
            collection_status=status, pages_fetched=pages_fetched,
            requests_used=requests_used, max_requests=max_requests,
            duplicates_skipped=duplicates_skipped, previous_retained=len(retained),
            images_folder=images_folder, from_cache=from_cache, stop_reason=stop_reason,
            api_error=api_error, page_diagnostics=page_diagnostics,
            sort_by=sort_by, expected_name=expected_name,
            restaurant_media=restaurant_media or previous_media,
            review_filter=review_filter,
            topics=_merge_topics(_previous_topics(previous_docs), topics),
            filter_runs=previous_runs + [run_record],
            next_page_token=last_next_token,
        )

    def save_cache_manifest(complete: bool) -> None:
        if from_cache or not save_raw_pages or not cache_pages:
            return
        save_json(
            {
                "data_id": data_id, "sort_by": sort_by, "review_filter": review_filter,
                "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "pagination_complete": complete, "last_next_page_token": last_next_token,
                "pages": cache_pages,
            },
            make_cache_manifest_path(data_id, review_filter),
        )

    try:
        page_num = 0
        while True:
            page_num += 1
            fetch_meta: dict = {}
            if from_cache:
                data = next(page_source, None)
                if data is None:
                    if last_next_token:
                        collection_status = STATUS_PARTIAL
                        stop_reason = "Cached pages end before pagination finished."
                    else:
                        collection_status = STATUS_COMPLETED if run_reviews else STATUS_NO_REVIEWS
                    break
                print(f"Loading cached review page {page_num}...")
            else:
                if requests_used >= max_requests:
                    collection_status = STATUS_REQUEST_LIMIT
                    stop_reason = (f"Reached the request budget of {max_requests}. "
                                   "Run again with a larger --max-requests to continue.")
                    print(f"\n  -> {stop_reason}")
                    break
                if requests_used > 0 and delay > 0:
                    time.sleep(delay)
                print(f"Fetching review page {page_num}...")
                error_type: Optional[str] = None
                try:
                    requests_used += 1
                    data = fetch_page(api_key, params, meta=fetch_meta)
                except InvalidApiKeyError as exc:
                    error_type, message = "invalid_api_key", str(exc)
                except CreditsExhaustedError as exc:
                    error_type, message = STATUS_QUOTA, str(exc)
                except (SerpApiError, RuntimeError) as exc:
                    error_type, message = STATUS_API_ERROR, str(exc) or "API request failed."
                if error_type:
                    message = redact_secrets(message, api_key)
                    api_error = {"type": error_type, "message": message, "page": page_num,
                                 "http_status": fetch_meta.get("http_status")}
                    stop_reason = message
                    if error_type == STATUS_QUOTA:
                        collection_status = STATUS_QUOTA
                    elif run_reviews:
                        collection_status = STATUS_PARTIAL
                    else:
                        collection_status = STATUS_API_ERROR
                    print(f"\n[ERROR] {message}")
                    if not run_reviews:
                        print("No reviews were collected. The failure is recorded in the output JSON.")
                    break

            pages_fetched += 1
            diag = describe_response(data, fetch_meta.get("http_status"))
            if fetch_meta.get("api_message"):
                diag["api_message"] = fetch_meta["api_message"]
            page_diagnostics.append({"page": pages_fetched, **diag})

            page_label = first_page - 1 + pages_fetched
            if save_raw_pages and not from_cache:
                raw_path = make_raw_page_path(data_id, page_label, review_filter)
                save_json(redact_secrets(data, api_key), raw_path)
                cache_pages.append(to_project_relative(raw_path))

            raw_reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict)]

            if page_label == 1:
                topics = extract_topics(data)
            if pages_fetched == 1:
                place_info = extract_place_info(data)
                ptype = f"  (type: {place_info['type']})" if place_info.get("type") else ""
                print(f"\nRestaurant : {place_info['restaurant_name']}{ptype}")
                if place_info.get("address"):
                    print(f"Address    : {place_info['address']}")
                if place_info.get("total_reviews_reported"):
                    print(f"Total reviews on Google Maps : {place_info['total_reviews_reported']}")
                if place_info.get("reviews_results_state"):
                    print(f"SerpApi reviews state        : {place_info['reviews_results_state']}")
                if place_info.get("place_warning"):
                    print(f"\n[WARNING] {place_info['place_warning']}")
                match = name_matches(place_info["restaurant_name"], expected_name)
                if match is False:
                    print(f"\n[WARNING] Expected '{expected_name}' but the API returned "
                          f"'{place_info['restaurant_name']}'. The data_id points to a different place.")
                if previous_reviews:
                    print(f"Previously saved reviews     : {len(previous_reviews)} (kept and merged)")
                print()

            if diagnostics or (pages_fetched == 1 and not raw_reviews):
                _print_diagnostics(pages_fetched, diag)
                print()

            page_new: list[dict] = []
            for raw in raw_reviews:
                review = flatten_review(raw)
                if review_filter:
                    _tag_review(review, [review_filter])
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
                    {"data_id": data_id, "page": page_label, "review_filter": review_filter,
                     "has_next_page": bool(last_next_token),
                     "reviews_on_page": len(raw_reviews), "new_reviews": len(page_new),
                     "diagnostics": diag,
                     "reviews": [{"review_id": r["review_id"], "author": r["author"], "rating": r["rating"],
                                  "date": r["date"], "images": r["images"]} for r in page_new]},
                    make_extracted_page_path(data_id, page_label, review_filter),
                )

            stats = _image_stats(list(run_reviews.values()))
            print(f"Reviews collected: {len(run_reviews)}")
            print(f"Reviews with images: {stats['reviews_with_images']}")
            print(f"Review images found: {stats['images_found']}")
            print(f"Images downloaded: {stats['images_downloaded']}")
            print()

            if not last_next_token:
                if run_reviews:
                    collection_status = STATUS_COMPLETED
                    print("  -> No next-page token: SerpApi offers no further review pages.")
                else:
                    collection_status = STATUS_NO_REVIEWS
                    print("  -> The API returned no reviews and no next-page token for this data_id.")
                break
            if not raw_reviews:
                collection_status = STATUS_PARTIAL
                stop_reason = "SerpApi returned an empty page that still had a next-page token."
                break

            params = {**base_params, "next_page_token": last_next_token, "num": REVIEWS_PER_PAGE}

            if save_every > 0 and pages_fetched % save_every == 0:
                save_json(assemble("in_progress"), output_path)
                save_cache_manifest(False)

    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] User cancelled. Saving collected reviews...")
        collection_status = STATUS_PARTIAL
        stop_reason = "Interrupted by user."

    # -----------------------------------------------------------------------
    # Customer reviews (and their photos) from the place details response
    # -----------------------------------------------------------------------
    # Place-details reviews are not filter matches, so filtered runs leave them out
    place_reviews = [] if review_filter else (restaurant_media or {}).get("place_reviews") or []
    for review in place_reviews:
        fp = make_review_fingerprint(review)
        if fp in run_reviews:
            _merge_images(run_reviews[fp], review)
            target = run_reviews[fp]
        else:
            run_reviews[fp] = review
            target = review
        if downloader is not None:
            downloader.process_review(target)
        else:
            for img in target.get("images") or []:
                img.setdefault("local_path", None)
                img.setdefault("download_status", "not_requested")
    place_review_images = sum(len(r.get("images") or []) for r in place_reviews)
    if place_review_images:
        print(f"Review photos from the place details' most relevant reviews: {place_review_images}")

    # -----------------------------------------------------------------------
    # Final save (also when the very first request failed: the error is kept)
    # -----------------------------------------------------------------------
    output = assemble(collection_status)
    save_json(output, output_path)
    save_cache_manifest(collection_status in SUCCESS_STATUSES)

    summary = output["image_summary"]
    main_photo = output.get("main_restaurant_photo")
    if summary["main_photo_downloaded"]:
        main_line = "Yes" + (" (labelled fallback listing photo)" if summary["main_photo_is_fallback"] else "")
    elif main_photo:
        main_line = f"No ({main_photo.get('download_status')})"
    else:
        main_line = f"No - {output['main_restaurant_photo_unavailable_reason']}"
    reported = output["total_reviews_reported"]

    print(f"\n{DIVIDER}")
    print("  IMAGE COLLECTION SUMMARY")
    print(f"{DIVIDER}")
    ptype = f" (type: {output['type']})" if output.get("type") else ""
    print(f"  Restaurant                    : {output['restaurant_name']}{ptype}")
    print(f"  Main listing photo downloaded : {main_line}")
    print(f"  Reviews collected             : {summary['reviews_collected']}"
          + (f" (of {reported} reported by Google)" if reported else ""))
    print(f"  Reviews containing photos     : {summary['reviews_with_images']}")
    print(f"  Review image URLs found       : {summary['review_images_found']}")
    print(f"  Review images downloaded      : {summary['review_images_downloaded']}")
    print(f"  Failed downloads              : {summary['review_image_download_failures']}"
          + (" (see download_status in JSON)" if summary["review_image_download_failures"] else ""))
    if output["restaurant_photos"]:
        print(f"  Other listing photos          : {len(output['restaurant_photos'])} (listed after review photos)")
    print(f"  Pages fetched                 : {pages_fetched}")
    print(f"  API requests used             : {output['api_requests_used_total']}"
          f" ({output['restaurant_details_requests_used']} restaurant details, {requests_used} review pages)")
    print(f"  JSON output                   : {to_project_relative(output_path)}")
    print(f"  Image folder                  : {images_folder + '/' if images_folder else 'n/a (downloads disabled)'}")
    print(f"{DIVIDER}")
    print(f"Collection status: {collection_status}")
    if output["review_coverage"]["note"]:
        print(f"COVERAGE: {output['review_coverage']['note']}")
    if summary["reviews_collected"] and not summary["review_images_found"]:
        print("NOTE: None of the reviews returned by the API has an 'images' field; "
              "no listing photos were substituted.")
    if stop_reason and collection_status not in SUCCESS_STATUSES:
        print(f"Reason: {stop_reason}")
    if output.get("place_warning"):
        print(f"WARNING: {output['place_warning']}")
    if collection_status in FAILURE_STATUSES:
        print("NOTE: The extraction FAILED — this is not an empty result.")
    elif collection_status not in SUCCESS_STATUSES:
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
    sort_by: str = SORT_BY,
    diagnostics: bool = False,
    expected_name: Optional[str] = None,
    restaurant_photos: bool = False,
    refresh_place: bool = False,
    photo_pages: int = 0,
    fresh: bool = False,
    query: Optional[str] = None,
    topic_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Load/validate the key (or the cache), then run the extraction.

    With *restaurant_photos*, the restaurant's main photo (and other listing
    photos) is looked up and downloaded FIRST, sharing the *max_requests*
    budget with the review pages (cached place details cost 0 credits).

    Returns None only when no search could be attempted (missing cache,
    rejected key or no credits at the free account check).
    """
    options = {"sort_by": sort_by, "diagnostics": diagnostics, "expected_name": expected_name,
               "query": query, "topic_id": topic_id}
    ensure_dirs()

    def media(api_key: Optional[str], budget: int) -> Optional[dict]:
        if not restaurant_photos:
            return None
        found = collect_restaurant_photos(
            api_key, data_id, downloader=downloader, max_requests=budget, from_cache=from_cache,
            refresh=refresh_place, photo_pages=photo_pages, fetch=fetch_page,
        )
        print()
        return found

    if from_cache:
        try:
            pages = list(iter_cached_pages(data_id, review_filter_tag(query, topic_id)))
        except (OSError, ValueError) as exc:
            print(f"[ERROR] Could not read cached pages: {exc}")
            return None
        if not pages:
            print("[ERROR] No cached responses found for this data_id. Run once without --from-cache.")
            return None
        return run_extraction(None, data_id, max_requests=0, downloader=downloader,
                              cached_pages=pages, save_raw_pages=save_raw_pages,
                              restaurant_media=media(None, 0), **options)

    api_key = load_api_key()
    if not skip_key_check:
        print("Checking SerpApi key (account check, uses no search credits)...")
        try:
            account = validate_api_key(api_key)
        except InvalidApiKeyError as exc:
            print(f"[ERROR] {exc}")
            print(f"Collection status: {STATUS_API_ERROR} (no search was made)")
            return None
        except CreditsExhaustedError as exc:
            print(f"[ERROR] {exc}")
            print(f"Collection status: {STATUS_QUOTA} (no search was made)")
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

    restaurant_media = media(api_key, max_requests)
    if restaurant_media and restaurant_media["api_requests_used"]:
        max_requests -= restaurant_media["api_requests_used"]
        print(f"Request budget left for review pages: {max_requests}\n")
    return run_extraction(api_key, data_id, max_requests=max_requests, downloader=downloader,
                          save_raw_pages=save_raw_pages, restaurant_media=restaurant_media, fresh=fresh,
                          **options)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Options shared by extract_reviews.py and extract_review_images.py."""
    parser.add_argument("--sort-by", choices=SORT_CHOICES, default=SORT_BY,
                        help=f"SerpApi review order (default: {SORT_BY}; Google's own default is qualityScore).")
    parser.add_argument("--expected-name", metavar="NAME", default=None,
                        help="Restaurant name you expect; a warning is shown if the API returns another place.")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Print a safe structural summary of every API response (no keys or URLs).")
    parser.add_argument("--no-main-photo", action="store_true",
                        help="Skip the restaurant details lookup (main restaurant photo); saves 1 credit.")
    parser.add_argument("--refresh-place", action="store_true",
                        help="Look up the restaurant details again instead of reusing the cached response.")
    parser.add_argument("--restaurant-photo-pages", type=int, default=0, metavar="N",
                        help="Also fetch N pages of restaurant photos from the Google Maps Photos API "
                             "(1 credit per page; default 0).")
    parser.add_argument("--fresh", action="store_true",
                        help="Ask SerpApi for a fresh search (no_cache=true) instead of its cached result. "
                             "Use when a response looks truncated (e.g. 5 reviews and no next page).")


def restaurant_photo_options(args: argparse.Namespace) -> dict:
    """run_cli keyword arguments for the restaurant-photo (and shared) options in *args*."""
    return {
        "restaurant_photos": not args.no_main_photo,
        "refresh_place":     args.refresh_place,
        "photo_pages":       max(0, args.restaurant_photo_pages),
        "fresh":             args.fresh,
    }


def exit_code(result: Optional[dict]) -> int:
    """0 for a finished or partially finished run, 1 when nothing could be fetched."""
    if result is None or result.get("collection_status") in FAILURE_STATUSES:
        return 1
    return 0


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
    review_filter = parser.add_mutually_exclusive_group()
    review_filter.add_argument("--query", metavar="TEXT", default=None,
                               help="Only fetch reviews matching TEXT (SerpApi 'query'). Saved to data/output/washroom/.")
    review_filter.add_argument("--topic-id", metavar="ID", default=None,
                               help="Only fetch reviews about this topic (SerpApi 'topic_id'; see the page-1 "
                                    "'topics' array). Saved to data/output/washroom/.")
    add_common_args(parser)
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
        sort_by=args.sort_by,
        diagnostics=args.diagnostics,
        expected_name=args.expected_name,
        query=args.query,
        topic_id=args.topic_id,
        **{**restaurant_photo_options(args),
           # A filtered run collects review text only: no place-details lookup
           **({"restaurant_photos": False} if args.query or args.topic_id else {})},
    )
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
