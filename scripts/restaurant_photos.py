"""
scripts/restaurant_photos.py
============================
Main restaurant photo (and other listing photos) for Pitstop.

The reviews response (``google_maps_reviews``) has no restaurant image, so
the restaurant's place details are looked up with SerpApi's Google Maps API
(``engine=google_maps``, ``data_cid`` derived from the same ``data_id``):

  - The result is used only if its ``data_id`` (or ``data_cid``) matches the
    requested ``data_id`` — another place's photos are never used.
  - The main photo is ``place_results.thumbnail`` (the listing's primary
    image). Without it, a listing category image is used and labelled as a
    fallback. Review photos are never used as the main photo.
  - Other listing photos come from ``place_results.images`` (no extra
    credit) and, optionally, from the Google Maps Photos API
    (``--restaurant-photo-pages``, 1 credit per page).
  - Responses are cached in ``data/raw_data/serpapi/`` (API key removed) and
    reused on later runs, so a restaurant is looked up only once.

Files are saved next to the review images in
``data/extracted_images/<data_id>/`` as ``restaurant_main.<ext>`` and
``restaurant_photo_<n>.<ext>``.
"""

from __future__ import annotations

import datetime
import time
from typing import Callable, Optional

try:
    from scripts import utils
    from scripts.utils import (
        CreditsExhaustedError,
        InvalidApiKeyError,
        SerpApiError,
        data_cid_from_data_id,
        extract_listing_photos,
        extract_place_user_reviews,
        extract_photos_api_photos,
        fetch_page,
        load_json,
        photo_key,
        redact_secrets,
        save_json,
        select_main_photo,
        verify_place_identity,
    )
except ImportError:
    import utils
    from utils import (
        CreditsExhaustedError,
        InvalidApiKeyError,
        SerpApiError,
        data_cid_from_data_id,
        extract_listing_photos,
        extract_place_user_reviews,
        extract_photos_api_photos,
        fetch_page,
        load_json,
        photo_key,
        redact_secrets,
        save_json,
        select_main_photo,
        verify_place_identity,
    )

MAIN_PHOTO_STEM = "restaurant_main"
RESTAURANT_PHOTO_STEM = "restaurant_photo"


def _api_error(exc: Exception, api_key: Optional[str]) -> dict:
    if isinstance(exc, InvalidApiKeyError):
        kind = "invalid_api_key"
    elif isinstance(exc, CreditsExhaustedError):
        kind = "quota_exhausted"
    else:
        kind = "api_error"
    return {"type": kind, "message": redact_secrets(str(exc) or "API request failed.", api_key)}


def _load_cache(path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (OSError, ValueError):
        print(f"  [WARN] Cached file {path.name} is unreadable; ignoring it.")
        return None


def collect_restaurant_photos(
    api_key: Optional[str],
    data_id: str,
    *,
    downloader=None,
    max_requests: int = 1,
    from_cache: bool = False,
    refresh: bool = False,
    photo_pages: int = 0,
    fetch: Callable[..., dict] = fetch_page,
    delay: float = utils.REQUEST_DELAY_SECONDS,
) -> dict:
    """
    Find, verify and download the restaurant's main photo, then its other
    listing photos. The main photo is always downloaded first.

    Args:
        api_key:      SerpApi key (not needed when the cache is used).
        data_id:      Google Maps restaurant ``data_id``.
        downloader:   ``ReviewImageDownloader`` shared with the review images
                      (same folder and manifest); None records URLs only.
        max_requests: SerpApi credits this step may spend.
        from_cache:   Never call the API; use cached responses only.
        refresh:      Ignore the cached place details and look up again.
        photo_pages:  Google Maps Photos API pages to fetch (0 = none).
        fetch:        SerpApi request function (injected by callers/tests).

    Returns:
        ``{"main_restaurant_photo", "main_restaurant_photo_unavailable_reason",
        "restaurant_photos", "place_reviews", "place_lookup", "api_requests_used"}``.
        ``place_reviews`` are customer reviews (with photos) from the place details.
    """
    lookup: dict = {
        "engine": "google_maps",
        "lookup_parameter": "data_cid",
        "data_cid": data_cid_from_data_id(data_id),
        "source": None,
        "identity_verified": False,
        "verified_by": None,
        "returned_title": None,
        "returned_data_id": None,
        "api_error": None,
        "photos_api_pages": 0,
    }
    result: dict = {
        "main_restaurant_photo": None,
        "main_restaurant_photo_unavailable_reason": None,
        "restaurant_photos": [],
        "place_reviews": [],
        "place_lookup": lookup,
        "api_requests_used": 0,
    }

    def spend() -> bool:
        if result["api_requests_used"] >= max_requests:
            return False
        if result["api_requests_used"] > 0 and delay > 0:
            time.sleep(delay)
        result["api_requests_used"] += 1
        return True

    # -- 1. place details (cache first) -----------------------------------
    cache_path = utils.make_place_cache_path(data_id)
    data = None if refresh and not from_cache else _load_cache(cache_path)
    if data is not None:
        lookup["source"] = "cache"
        print("Restaurant details : cached place details (0 API credits)")
    elif from_cache:
        result["main_restaurant_photo_unavailable_reason"] = (
            "No cached place details for this data_id; run once without --from-cache to look them up.")
    elif not spend():
        result["main_restaurant_photo_unavailable_reason"] = (
            "The API request budget left no credit for the restaurant details lookup.")
    else:
        print("Fetching restaurant details (main photo)...")
        try:
            data = fetch(api_key, {"engine": "google_maps", "data_cid": lookup["data_cid"], "hl": "en"})
        except (SerpApiError, RuntimeError) as exc:
            lookup["api_error"] = _api_error(exc, api_key)
            result["main_restaurant_photo_unavailable_reason"] = (
                f"Restaurant details lookup failed ({lookup['api_error']['type']}): {lookup['api_error']['message']}")
            print(f"  [WARN] {result['main_restaurant_photo_unavailable_reason']}")
        else:
            lookup["source"] = "api"
            save_json(redact_secrets(data, api_key), cache_path)

    if data is None:
        return result

    place = data.get("place_results") if isinstance(data.get("place_results"), dict) else None
    if place is None:
        result["main_restaurant_photo_unavailable_reason"] = (
            "The Google Maps response has no 'place_results' for this data_cid.")
        print(f"  [WARN] {result['main_restaurant_photo_unavailable_reason']}")
        return result

    verified, field = verify_place_identity(place, data_id)
    lookup.update({
        "identity_verified": verified, "verified_by": field,
        "returned_title": place.get("title"), "returned_data_id": place.get("data_id"),
    })
    if not verified:
        result["main_restaurant_photo_unavailable_reason"] = (
            f"The place returned ({place.get('title')!r}, data_id {place.get('data_id')!r}) could not be "
            f"verified as {data_id}; its photos were not used.")
        print(f"  [WARN] {result['main_restaurant_photo_unavailable_reason']}")
        return result

    # Customer reviews (with their attached photos) embedded in the place details;
    # downloaded later with the other review photos so the main photo stays first
    result["place_reviews"] = extract_place_user_reviews(place)
    lookup["place_reviews_with_images"] = sum(1 for r in result["place_reviews"] if r["images"])

    # -- 2. main photo — downloaded before anything else ------------------
    main, reason = select_main_photo(place)
    result["main_restaurant_photo_unavailable_reason"] = reason
    if main is not None:
        _download(downloader, main, MAIN_PHOTO_STEM)
        result["main_restaurant_photo"] = main
        label = " (fallback)" if main["is_fallback"] else ""
        print(f"Main photo{label}     : {main.get('local_path') or main['download_status']}")
    else:
        print(f"  [WARN] Main restaurant photo unavailable: {reason}")

    # -- 3. other listing photos ------------------------------------------
    seen = {photo_key(main["image_url"])} if main else set()
    photos = extract_listing_photos(place, seen)
    seen |= {photo_key(p["image_url"]) for p in photos}
    if photo_pages > 0:
        photos += _photos_api(api_key, data_id, photo_pages, seen, lookup, from_cache, refresh, fetch, spend)

    for n, photo in enumerate(photos, 1):
        _download(downloader, photo, f"{RESTAURANT_PHOTO_STEM}_{n}")
    result["restaurant_photos"] = photos
    if photos:
        print(f"Restaurant photos  : {len(photos)}")
    return result


def _download(downloader, entry: dict, file_stem: str) -> None:
    if downloader is None:
        entry.setdefault("local_path", None)
        entry.setdefault("download_status", "not_requested")
    else:
        downloader.process_restaurant_image(entry, file_stem)


def _photos_api(api_key, data_id, max_pages, seen, lookup, from_cache, refresh, fetch, spend) -> list[dict]:
    """Restaurant photos from the Google Maps Photos API (cached; 1 credit per page)."""
    cache_path = utils.make_photos_cache_path(data_id)
    cached = None if refresh and not from_cache else _load_cache(cache_path)
    pages: list[dict] = list((cached or {}).get("pages") or [])
    token = (cached or {}).get("next_page_token")
    fetched = False

    while not from_cache and len(pages) < max_pages and (token or not pages):
        if not spend():
            print("  [WARN] Request budget reached before all restaurant photo pages were fetched.")
            break
        params = {"engine": "google_maps_photos", "data_id": data_id, "hl": "en"}
        if token:
            params["next_page_token"] = token
        try:
            page = redact_secrets(fetch(api_key, params), api_key)
        except (SerpApiError, RuntimeError) as exc:
            lookup["photos_api_error"] = _api_error(exc, api_key)
            print(f"  [WARN] Photos API failed: {lookup['photos_api_error']['message']}")
            break
        pages.append(page)
        fetched = True
        token = (page.get("serpapi_pagination") or {}).get("next_page_token")

    if fetched:
        save_json({"data_id": data_id, "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "next_page_token": token, "pages": pages}, cache_path)

    photos: list[dict] = []
    for page in pages[:max_pages]:
        new = extract_photos_api_photos(page, seen)
        seen |= {photo_key(p["image_url"]) for p in new}
        photos += new
    lookup["photos_api_pages"] = min(len(pages), max_pages)
    return photos


def build_image_gallery(main: Optional[dict], restaurant_photos: list[dict], reviews: list[dict]) -> list[dict]:
    """
    Ordered gallery: main listing photo, then customer review photos (each
    keeping its ``review_id``), then any other listing photos. Without a main
    photo the review photos come first; none is promoted to main. A file or
    URL already listed is not repeated (``reviews[].images`` keep every link).
    """
    gallery: list[dict] = []
    seen: set[str] = set()

    def add(img: dict, image_type: str, **extra) -> None:
        key = img.get("local_path") or img.get("image_url")
        if not key or key in seen:
            return
        seen.add(key)
        gallery.append({
            "position": len(gallery) + 1, "image_type": image_type,
            "image_url": img.get("image_url"), "local_path": img.get("local_path"),
            "download_status": img.get("download_status"), **extra,
        })

    if main:
        add(main, utils.IMAGE_TYPE_MAIN)
    for review in reviews:
        for img in review.get("images") or []:
            add(img, utils.IMAGE_TYPE_REVIEW, review_id=review.get("review_id"), image_index=img.get("image_index"))
    for photo in restaurant_photos:
        add(photo, utils.IMAGE_TYPE_RESTAURANT)
    return gallery
