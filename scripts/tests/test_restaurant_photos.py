"""
scripts/tests/test_restaurant_photos.py
=======================================
Tests for the main restaurant photo (scripts/restaurant_photos.py) and its
integration with the review / review-image pipeline.

Covers:
  - Main photo taken from place_results.thumbnail, looked up by data_cid
  - Restaurant identity verified (data_id / data_cid) before photos are used
  - Fallback main photo is labelled; review photos are never the main photo
  - Main photo downloaded before review photos, saved as restaurant_main.<ext>
  - Gallery order: main photo -> review photos -> other listing photos
  - Restaurant and review images kept in separate categories
  - Missing main photo, failed lookups and failed downloads handled safely
  - Duplicate photos (other sizes, same URL, same bytes) handled
  - Cached place details reused (0 credits); request budget respected
  - Existing review pagination still works

No real network access: SerpApi calls and image downloads are mocked.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
_ROOT = _SCRIPTS.parent
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import extract_review_images as eri
    from scripts import extract_reviews, restaurant_photos as rp, utils
except ImportError:
    import extract_review_images as eri
    import extract_reviews
    import restaurant_photos as rp
    import utils

DATA_ID = "0x3bae19cd83892495:0xc4ee26c182749dfe"
SECRET_KEY = "a1b2c3d4" * 8
MAIN_URL = "https://lh5.googleusercontent.com/p/MAINPHOTO=w129-h86-k-no"
MAIN_URL_LARGE = "https://lh5.googleusercontent.com/p/MAINPHOTO=w447-h298-k-no"
MENU_URL = "https://lh5.googleusercontent.com/p/MENUPHOTO=w447-h298-k-no"
LATEST_URL = "https://lh5.googleusercontent.com/p/LATESTPHOTO=w447-h298-k-no"
REVIEW_URL_1 = "https://lh3.googleusercontent.com/gps-cs-s/REVIEW1=w600"
REVIEW_URL_2 = "https://lh3.googleusercontent.com/gps-cs-s/REVIEW2=w600"

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


def place_response(*, thumbnail=MAIN_URL, images=None, data_id=DATA_ID, title="The Food Factory", **extra):
    place = {"title": title, "data_id": data_id, "data_cid": utils.data_cid_from_data_id(DATA_ID),
             "address": "International Airport Road", "rating": 4.4, **extra}
    if thumbnail is not None:
        place["thumbnail"] = thumbnail
    place["images"] = images if images is not None else [
        {"title": "All", "thumbnail": MAIN_URL_LARGE},
        {"title": "Menu", "thumbnail": MENU_URL},
        {"title": "Latest", "thumbnail": LATEST_URL},
    ]
    return {"search_metadata": {"status": "Success"}, "place_results": place}


def reviews_page(reviews, next_token=None):
    return {
        "search_metadata": {"status": "Success"},
        "place_info": {"title": "The Food Factory", "address": "International Airport Road",
                       "rating": 4.4, "reviews": 3193, "type": "Eclectic restaurant"},
        "reviews": reviews,
        "serpapi_pagination": {"next_page_token": next_token} if next_token else {},
    }


def review(review_id, urls):
    return {"review_id": review_id, "user": {"name": f"User {review_id}", "thumbnail": "https://avatar.test/a"},
            "rating": 5, "snippet": f"Review {review_id} text", "images": urls}


class _Session:
    """Image server: URL -> bytes (200), or (status, content_type, body). Records call order."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        route = self.routes.get(url, (404, "text/html", b"<html>404</html>"))
        status, ctype, body = route if isinstance(route, tuple) else (200, "", route)

        class R:
            status_code = status
            headers = {"Content-Type": ctype} if ctype else {}

            def iter_content(self_inner, chunk_size=65536):
                yield body

            def close(self_inner):
                pass

        return R()


def default_routes():
    return {MAIN_URL: JPEG + b"main", MENU_URL: PNG + b"menu", LATEST_URL: JPEG + b"latest",
            REVIEW_URL_1: JPEG + b"review1", REVIEW_URL_2: JPEG + b"review2"}


class _Base(unittest.TestCase):
    """Redirects every data/ path into a temporary project root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        data = self.root / "data"
        self._patches = [
            patch.object(utils, "PROJECT_ROOT", self.root),
            patch.object(utils, "DATA_DIR", data),
            patch.object(utils, "RAW_DATA_DIR", data / "raw_data"),
            patch.object(utils, "EXTRACTED_PAGES_DIR", data / "extracted_pages"),
            patch.object(utils, "EXTRACTED_IMAGES_DIR", data / "extracted_images"),
            patch.object(utils, "OUTPUT_DIR", data / "output"),
            patch.object(utils, "PROCESSED_DIR", data / "processed"),
            patch.object(utils, "SOURCES_DIR", self.root / "sources"),
            patch.object(eri.time, "sleep"),
            patch.object(rp.time, "sleep"),
            patch.object(extract_reviews.time, "sleep"),
        ]
        for p in self._patches:
            p.start()
        self.api_calls: list[dict] = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    @property
    def folder(self):
        return self.root / "data" / "extracted_images" / utils.safe_data_id(DATA_ID)

    def fake_api(self, place=None, review_pages=None, photos_pages=None):
        """Route mocked SerpApi calls by engine; items may be dicts or exceptions."""
        queues = {"google_maps": [place if place is not None else place_response()],
                  "google_maps_reviews": list(review_pages or [reviews_page([])]),
                  "google_maps_photos": list(photos_pages or [])}

        def fetch(api_key, params, **kw):
            self.assertEqual(api_key, SECRET_KEY)
            self.api_calls.append(dict(params))
            queue = queues[params["engine"]]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, BaseException):
                raise item
            return item
        return fetch

    def run_cli(self, *, place=None, review_pages=None, photos_pages=None, routes=None,
                max_requests=5, photo_pages=0, from_cache=False, restaurant_photos=True, refresh=False,
                fresh=False):
        self.session = _Session(routes if routes is not None else default_routes())
        downloader = eri.ReviewImageDownloader(DATA_ID, session=self.session, delay=0)
        with patch.object(extract_reviews, "load_api_key", return_value=SECRET_KEY), \
             patch.object(extract_reviews, "validate_api_key", return_value=None), \
             patch.object(extract_reviews, "fetch_page",
                          side_effect=self.fake_api(place, review_pages, photos_pages)), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            out = extract_reviews.run_cli(
                DATA_ID, max_requests=max_requests, downloader=downloader, from_cache=from_cache,
                restaurant_photos=restaurant_photos, refresh_place=refresh, photo_pages=photo_pages,
                fresh=fresh,
            )
        self.stdout = buf.getvalue()
        return out

    def saved_output(self):
        files = list((self.root / "data" / "output").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text(encoding="utf-8"))

    def engines(self):
        return [c["engine"] for c in self.api_calls]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_data_cid_is_decimal_of_second_part(self):
        self.assertEqual(utils.data_cid_from_data_id("0x1:0xff"), "255")
        self.assertEqual(utils.data_cid_from_data_id(DATA_ID), str(0xc4ee26c182749dfe))

    def test_identity_verified_by_data_id_case_insensitive(self):
        self.assertEqual(utils.verify_place_identity({"data_id": DATA_ID.upper()}, DATA_ID), (True, "data_id"))

    def test_identity_rejected_for_other_place(self):
        verified, field = utils.verify_place_identity({"data_id": "0x1:0x2", "title": "The Food Factory"}, DATA_ID)
        self.assertFalse(verified)
        self.assertEqual(field, "data_id")

    def test_identity_falls_back_to_data_cid(self):
        cid = utils.data_cid_from_data_id(DATA_ID)
        self.assertEqual(utils.verify_place_identity({"data_cid": cid}, DATA_ID), (True, "data_cid"))
        self.assertEqual(utils.verify_place_identity({"data_cid": "123"}, DATA_ID), (False, "data_cid"))

    def test_name_alone_never_verifies(self):
        self.assertEqual(utils.verify_place_identity({"title": "The Food Factory"}, DATA_ID), (False, None))

    def test_main_photo_uses_explicit_thumbnail_field(self):
        photo, reason = utils.select_main_photo(place_response()["place_results"])
        self.assertIsNone(reason)
        self.assertEqual(photo["image_url"], MAIN_URL)
        self.assertEqual(photo["source_field"], "place_results.thumbnail")
        self.assertFalse(photo["is_fallback"])
        self.assertEqual(photo["image_type"], "main_restaurant_photo")

    def test_fallback_prefers_all_category_and_is_labelled(self):
        place = place_response(thumbnail=None, images=[{"title": "Menu", "thumbnail": MENU_URL},
                                                       {"title": "All", "thumbnail": MAIN_URL_LARGE}])
        photo, _ = utils.select_main_photo(place["place_results"])
        self.assertEqual(photo["image_url"], MAIN_URL_LARGE)
        self.assertTrue(photo["is_fallback"])
        self.assertEqual(photo["selection_method"], "fallback_listing_category_thumbnail")

    def test_no_photo_returns_reason(self):
        photo, reason = utils.select_main_photo(place_response(thumbnail=None, images=[])["place_results"])
        self.assertIsNone(photo)
        self.assertIn("no 'thumbnail'", reason)

    def test_photo_key_ignores_size_suffix(self):
        self.assertEqual(utils.photo_key(MAIN_URL), utils.photo_key(MAIN_URL_LARGE))
        self.assertNotEqual(utils.photo_key(MAIN_URL), utils.photo_key(MENU_URL))

    def test_listing_photos_exclude_main_and_duplicates(self):
        place = place_response(images=[{"title": "All", "thumbnail": MAIN_URL_LARGE},
                                       {"title": "Menu", "thumbnail": MENU_URL},
                                       {"title": "Menu again", "thumbnail": MENU_URL},
                                       {"title": "Broken", "thumbnail": "not-a-url"}])["place_results"]
        photos = utils.extract_listing_photos(place, {utils.photo_key(MAIN_URL)})
        self.assertEqual([p["image_url"] for p in photos], [MENU_URL])
        self.assertEqual(photos[0]["image_type"], "restaurant_photo")

    def test_review_images_are_labelled_as_review_photos(self):
        imgs = utils.extract_review_images({"images": [REVIEW_URL_1]})
        self.assertEqual(imgs[0]["image_type"], "customer_review_photo")


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

class TestMainPhotoPipeline(_Base):

    def test_main_photo_retrieved_via_data_cid_and_saved(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertEqual(self.api_calls[0], {"engine": "google_maps", "hl": "en",
                                             "data_cid": utils.data_cid_from_data_id(DATA_ID)})
        main = out["main_restaurant_photo"]
        self.assertEqual(main["image_url"], MAIN_URL)
        self.assertEqual(main["download_status"], "downloaded")
        self.assertEqual(main["local_path"], f"data/extracted_images/{utils.safe_data_id(DATA_ID)}/restaurant_main.jpg")
        self.assertTrue((self.folder / "restaurant_main.jpg").exists())
        self.assertTrue(out["place_lookup"]["identity_verified"])
        self.assertTrue(out["place_lookup"]["title_matches_reviews_place"])
        self.assertIsNone(out["main_restaurant_photo_unavailable_reason"])

    def test_extension_comes_from_image_content(self):
        routes = {**default_routes(), MAIN_URL: (200, "image/png", PNG + b"main")}
        out = self.run_cli(routes=routes)
        self.assertTrue(out["main_restaurant_photo"]["local_path"].endswith("restaurant_main.png"))

    def test_main_photo_downloaded_before_review_photos(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1, REVIEW_URL_2])])])
        self.assertEqual(self.session.calls[0], MAIN_URL)
        self.assertLess(self.session.calls.index(MAIN_URL), self.session.calls.index(REVIEW_URL_1))
        self.assertEqual(self.engines()[0], "google_maps")

    def test_gallery_order_main_then_reviews_then_listing_photos(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1]), review("r2", [REVIEW_URL_2])])])
        gallery = out["image_gallery"]
        self.assertEqual([g["position"] for g in gallery], list(range(1, len(gallery) + 1)))
        self.assertEqual([g["image_type"] for g in gallery],
                         ["main_restaurant_photo", "customer_review_photo", "customer_review_photo",
                          "restaurant_photo", "restaurant_photo"])
        self.assertEqual(gallery[0]["image_url"], MAIN_URL)
        self.assertEqual(gallery[0]["local_path"], out["main_restaurant_photo"]["local_path"])
        self.assertEqual([g["review_id"] for g in gallery[1:3]], ["r1", "r2"])
        self.assertTrue(all(g["local_path"].endswith((f"review_{g['review_id']}_1.jpg")) for g in gallery[1:3]))

    def test_json_document_key_order(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        keys = list(self.saved_output())
        self.assertEqual(keys[:6], ["restaurant_name", "data_id", "main_restaurant_photo", "image_gallery",
                                    "reviews", "image_summary"])

    def test_output_file_is_restaurant_id_reviews_json(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        files = [p.name for p in (self.root / "data" / "output").glob("*.json")]
        self.assertEqual(files, [f"{utils.safe_data_id(DATA_ID)}_reviews.json"])

    def test_image_summary_excludes_main_photo(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1, REVIEW_URL_2]), review("r2", [])])])
        self.assertEqual(out["image_summary"], {
            "main_photo_downloaded": True, "main_photo_is_fallback": False,
            "reviews_collected": 2, "reviews_with_images": 1,
            "review_images_found": 2, "unique_review_image_urls": 2,
            "review_images_downloaded": 2, "unique_review_image_files": 2,
            "review_image_download_failures": 0,
        })
        self.assertIn("Main listing photo downloaded : Yes", self.stdout)
        self.assertIn("Review image URLs found       : 2", self.stdout)
        self.assertIn("Pages fetched                 : 1", self.stdout)
        self.assertIn("API requests used             : 2", self.stdout)

    def test_review_photos_stay_linked_to_their_reviews(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1]), review("r2", [REVIEW_URL_2]),
                                                       review("r3", [])])])
        by_id = {r["review_id"]: r for r in out["reviews"]}
        self.assertEqual([i["image_url"] for i in by_id["r1"]["images"]], [REVIEW_URL_1])
        self.assertEqual([i["image_url"] for i in by_id["r2"]["images"]], [REVIEW_URL_2])
        self.assertEqual(by_id["r3"]["images"], [])
        self.assertTrue(by_id["r1"]["images"][0]["local_path"].endswith("review_r1_1.jpg"))

    def test_restaurant_and_review_images_are_separate(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        restaurant_urls = {p["image_url"] for p in out["restaurant_photos"]} | {out["main_restaurant_photo"]["image_url"]}
        review_urls = {i["image_url"] for r in out["reviews"] for i in r["images"]}
        self.assertFalse(restaurant_urls & review_urls)
        self.assertTrue(all(p["image_type"] == "restaurant_photo" for p in out["restaurant_photos"]))
        self.assertTrue(all(i["image_type"] == "customer_review_photo" for r in out["reviews"] for i in r["images"]))
        names = sorted(p.name for p in self.folder.glob("*.*") if not p.name.startswith("_"))
        self.assertEqual(names, ["restaurant_main.jpg", "restaurant_photo_1.png", "restaurant_photo_2.jpg",
                                 "review_r1_1.jpg"])

    def test_existing_review_pagination_still_works(self):
        pages = [reviews_page([review("r1", [REVIEW_URL_1])], next_token="t1"),
                 reviews_page([review("r2", [REVIEW_URL_2])])]
        out = self.run_cli(review_pages=pages)
        self.assertEqual(self.engines(), ["google_maps", "google_maps_reviews", "google_maps_reviews"])
        self.assertEqual(self.api_calls[2]["next_page_token"], "t1")
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual({r["review_id"] for r in out["reviews"]}, {"r1", "r2"})
        self.assertEqual(out["api_requests_used"], 2)
        self.assertEqual(out["api_requests_used_total"], 3)

    def test_api_key_never_written(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        written = "\n".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*.json"))
        self.assertNotIn(SECRET_KEY, written)
        self.assertNotIn(SECRET_KEY, self.stdout)


class TestCorrectRestaurant(_Base):

    def test_other_place_is_rejected(self):
        out = self.run_cli(place=place_response(data_id="0x1:0x2", title="Some Other Place"),
                           review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertIsNone(out["main_restaurant_photo"])
        self.assertEqual(out["restaurant_photos"], [])
        self.assertFalse(out["place_lookup"]["identity_verified"])
        self.assertIn("could not be verified", out["main_restaurant_photo_unavailable_reason"])
        self.assertNotIn(MAIN_URL, self.session.calls)

    def test_response_without_place_results(self):
        out = self.run_cli(place={"search_metadata": {"status": "Success"}, "local_results": [{"title": "x"}]})
        self.assertIsNone(out["main_restaurant_photo"])
        self.assertIn("place_results", out["main_restaurant_photo_unavailable_reason"])


class TestMissingMainPhoto(_Base):

    def test_no_photo_in_listing_gives_null_and_reason(self):
        out = self.run_cli(place=place_response(thumbnail=None, images=[]),
                           review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertIsNone(out["main_restaurant_photo"])
        self.assertTrue(out["main_restaurant_photo_unavailable_reason"])
        self.assertEqual(out["image_gallery"][0]["image_type"], "customer_review_photo")

    def test_review_photo_never_promoted_to_main(self):
        out = self.run_cli(place=place_response(thumbnail=None, images=[]),
                           review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertIsNone(out["main_restaurant_photo"])
        self.assertFalse(any(g["image_type"] == "main_restaurant_photo" for g in out["image_gallery"]))

    def test_fallback_main_photo_labelled_in_output(self):
        out = self.run_cli(place=place_response(thumbnail=None))
        self.assertTrue(out["main_restaurant_photo"]["is_fallback"])
        self.assertEqual(out["main_restaurant_photo"]["image_url"], MAIN_URL_LARGE)

    def test_quota_error_reported_and_reviews_still_run(self):
        out = self.run_cli(place=utils.CreditsExhaustedError("SerpApi credits exhausted."),
                           review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertIsNone(out["main_restaurant_photo"])
        self.assertEqual(out["place_lookup"]["api_error"]["type"], "quota_exhausted")
        self.assertIn("quota_exhausted", out["main_restaurant_photo_unavailable_reason"])
        self.assertEqual(out["reviews"][0]["review_id"], "r1")

    def test_failed_main_download_keeps_json_valid(self):
        routes = {**default_routes(), MAIN_URL: (403, "text/html", b"<html>denied</html>")}
        out = self.run_cli(routes=routes, review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        saved = self.saved_output()
        main = saved["main_restaurant_photo"]
        self.assertEqual(main["image_url"], MAIN_URL)
        self.assertEqual(main["download_status"], "failed")
        self.assertIsNone(main["local_path"])
        self.assertEqual(saved["image_gallery"][0]["download_status"], "failed")
        self.assertEqual(saved["reviews"][0]["images"][0]["download_status"], "downloaded")
        self.assertFalse(list(self.folder.glob("restaurant_main*")))
        self.assertFalse(list(self.folder.glob("*.part")))
        self.assertEqual(out["reviews_collected"], 1)

    def test_html_body_not_saved_as_main_photo(self):
        routes = {**default_routes(), MAIN_URL: (200, "image/jpeg", b"<html>not an image</html>")}
        out = self.run_cli(routes=routes)
        self.assertEqual(out["main_restaurant_photo"]["download_status"], "not_an_image")
        self.assertFalse(list(self.folder.glob("restaurant_main*")))


class TestDuplicates(_Base):

    def test_resized_copy_of_main_not_listed_again(self):
        out = self.run_cli()
        urls = [p["image_url"] for p in out["restaurant_photos"]]
        self.assertNotIn(MAIN_URL_LARGE, urls)
        self.assertEqual(urls, [MENU_URL, LATEST_URL])

    def test_review_reusing_main_photo_url_not_duplicated(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [MAIN_URL])])])
        self.assertEqual(out["reviews"][0]["images"][0]["local_path"], out["main_restaurant_photo"]["local_path"])
        self.assertEqual(self.session.calls.count(MAIN_URL), 1)
        self.assertEqual(sum(1 for g in out["image_gallery"] if g["image_url"] == MAIN_URL), 1)
        self.assertEqual(out["image_gallery"][0]["image_type"], "main_restaurant_photo")

    def test_identical_bytes_stored_once(self):
        routes = {**default_routes(), REVIEW_URL_1: JPEG + b"main"}
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])], routes=routes)
        self.assertEqual(out["reviews"][0]["images"][0]["local_path"], out["main_restaurant_photo"]["local_path"])
        self.assertFalse((self.folder / "review_r1_1.jpg").exists())

    def test_existing_different_main_file_not_overwritten(self):
        self.folder.mkdir(parents=True)
        (self.folder / "restaurant_main.jpg").write_bytes(JPEG + b"older photo")
        out = self.run_cli()
        self.assertEqual((self.folder / "restaurant_main.jpg").read_bytes(), JPEG + b"older photo")
        self.assertTrue(out["main_restaurant_photo"]["local_path"].endswith("restaurant_main_2.jpg"))


class TestCreditsAndCache(_Base):

    def test_second_run_reuses_cached_place_and_files(self):
        self.run_cli()
        self.api_calls.clear()
        out = self.run_cli()
        self.assertEqual(self.engines(), ["google_maps_reviews"])
        self.assertEqual(out["place_lookup"]["source"], "cache")
        self.assertEqual(out["restaurant_details_requests_used"], 0)
        self.assertEqual(out["main_restaurant_photo"]["download_status"], "already_downloaded")
        self.assertNotIn(MAIN_URL, self.session.calls)

    def test_refresh_place_looks_up_again(self):
        self.run_cli()
        self.api_calls.clear()
        self.run_cli(refresh=True)
        self.assertEqual(self.engines()[0], "google_maps")

    def test_from_cache_uses_no_credits(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.api_calls.clear()
        out = self.run_cli(from_cache=True)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(out["main_restaurant_photo"]["image_url"], MAIN_URL)
        self.assertEqual(out["api_requests_used_total"], 0)

    def test_lookup_shares_the_request_budget(self):
        pages = [reviews_page([review(f"r{i}", [])], next_token=f"t{i}") for i in range(5)]
        out = self.run_cli(review_pages=pages, max_requests=3)
        self.assertEqual(len(self.api_calls), 3)
        self.assertEqual(self.engines(), ["google_maps", "google_maps_reviews", "google_maps_reviews"])
        self.assertEqual(out["collection_status"], "stopped_at_request_limit")

    def test_no_budget_left_for_lookup(self):
        media = rp.collect_restaurant_photos(SECRET_KEY, DATA_ID, max_requests=0,
                                             fetch=self.fake_api())
        self.assertEqual(self.api_calls, [])
        self.assertIsNone(media["main_restaurant_photo"])
        self.assertIn("budget", media["main_restaurant_photo_unavailable_reason"])

    def test_skipping_main_photo_keeps_earlier_result(self):
        self.run_cli()
        self.api_calls.clear()
        out = self.run_cli(restaurant_photos=False)
        self.assertNotIn("google_maps", self.engines())
        self.assertEqual(out["main_restaurant_photo"]["image_url"], MAIN_URL)

    def test_photos_api_is_opt_in_and_deduplicated(self):
        photos_page = {"photos": [{"image": MENU_URL}, {"image": "https://lh5.googleusercontent.com/p/EXTRA=w1"}],
                       "serpapi_pagination": {}}
        routes = {**default_routes(), "https://lh5.googleusercontent.com/p/EXTRA=w1": JPEG + b"extra"}
        out = self.run_cli(photos_pages=[photos_page], photo_pages=1, routes=routes)
        self.assertIn("google_maps_photos", self.engines())
        urls = [p["image_url"] for p in out["restaurant_photos"]]
        self.assertEqual(urls.count(MENU_URL), 1)
        self.assertIn("https://lh5.googleusercontent.com/p/EXTRA=w1", urls)
        self.assertEqual(out["restaurant_photos"][-1]["source_field"], "google_maps_photos.photos")

    def test_photos_api_not_called_by_default(self):
        self.run_cli()
        self.assertNotIn("google_maps_photos", self.engines())


# ---------------------------------------------------------------------------
# Main photo first, then every customer-review photo
# ---------------------------------------------------------------------------

REVIEW_URL_3 = "https://lh3.googleusercontent.com/gps-cs-s/REVIEW3=w600"
SHARED_URL = "https://lh3.googleusercontent.com/gps-cs-s/SHARED=w600"


def review_routes(*urls):
    return {**default_routes(), **{u: JPEG + u.encode() for u in urls}}


class TestReviewPhotoCollection(_Base):

    def test_multiple_photos_on_one_review_preserved_in_order(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1, REVIEW_URL_2, REVIEW_URL_3])])],
                           routes=review_routes(REVIEW_URL_3))
        images = out["reviews"][0]["images"]
        self.assertEqual([i["image_url"] for i in images], [REVIEW_URL_1, REVIEW_URL_2, REVIEW_URL_3])
        self.assertEqual([Path(i["local_path"]).name for i in images],
                         ["review_r1_1.jpg", "review_r1_2.jpg", "review_r1_3.jpg"])
        gallery = out["image_gallery"]
        self.assertEqual(gallery[0]["image_type"], "main_restaurant_photo")
        self.assertEqual([(g["review_id"], g["image_index"]) for g in gallery[1:4]], [("r1", 1), ("r1", 2), ("r1", 3)])

    def test_reviews_without_photos_handled(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", []), {"review_id": "r2", "snippet": "no image key"}])])
        self.assertEqual([r["images"] for r in out["reviews"]], [[], []])
        self.assertEqual(out["image_summary"]["review_images_found"], 0)
        self.assertEqual([g["image_type"] for g in out["image_gallery"]],
                         ["main_restaurant_photo", "restaurant_photo", "restaurant_photo"])
        self.assertIn("None of the reviews returned by the API has an 'images' field", self.stdout)

    def test_shared_photo_downloaded_once_but_linked_to_both_reviews(self):
        out = self.run_cli(review_pages=[reviews_page([review("r1", [SHARED_URL]), review("r2", [SHARED_URL, REVIEW_URL_1])])],
                           routes=review_routes(SHARED_URL))
        self.assertEqual(self.session.calls.count(SHARED_URL), 1)
        by_id = {r["review_id"]: r for r in out["reviews"]}
        self.assertEqual(by_id["r1"]["images"][0]["local_path"], by_id["r2"]["images"][0]["local_path"])
        self.assertEqual(by_id["r2"]["images"][0]["download_status"], "already_downloaded")
        self.assertEqual([g["image_url"] for g in out["image_gallery"]].count(SHARED_URL), 1)
        self.assertEqual(out["image_summary"]["review_images_found"], 3)
        self.assertEqual(out["image_summary"]["unique_review_image_files"], 2)

    def test_failed_review_download_keeps_json_valid(self):
        routes = {**default_routes(), REVIEW_URL_1: (404, "text/html", b"<html>gone</html>")}
        self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1, REVIEW_URL_2])])], routes=routes)
        saved = self.saved_output()
        imgs = saved["reviews"][0]["images"]
        self.assertEqual([i["download_status"] for i in imgs], ["failed", "downloaded"])
        self.assertIsNone(imgs[0]["local_path"])
        self.assertEqual(imgs[0]["image_url"], REVIEW_URL_1)
        self.assertEqual(saved["image_summary"]["review_image_download_failures"], 1)
        self.assertEqual(saved["image_summary"]["review_images_downloaded"], 1)
        self.assertEqual(saved["image_gallery"][0]["image_type"], "main_restaurant_photo")
        self.assertFalse(list(self.folder.glob("*.part")) + list(self.folder.glob(".*.part")))
        self.assertIn("Failed downloads              : 1", self.stdout)

    def test_pagination_collects_photos_from_every_page(self):
        pages = [reviews_page([review("r1", [REVIEW_URL_1])], next_token="t1"),
                 reviews_page([review("r2", [])], next_token="t2"),
                 reviews_page([review("r3", [REVIEW_URL_2, REVIEW_URL_3])])]
        out = self.run_cli(review_pages=pages, routes=review_routes(REVIEW_URL_3))
        review_calls = [c for c in self.api_calls if c["engine"] == "google_maps_reviews"]
        self.assertEqual([c.get("next_page_token") for c in review_calls], [None, "t1", "t2"])
        self.assertEqual(out["pages_fetched"], 3)
        self.assertEqual([g["review_id"] for g in out["image_gallery"][1:4]], ["r1", "r3", "r3"])
        self.assertEqual(out["image_summary"]["review_images_downloaded"], 3)

    def test_request_budget_stops_pagination(self):
        pages = [reviews_page([review("r1", [REVIEW_URL_1])], next_token="t1"),
                 reviews_page([review("r2", [REVIEW_URL_2])], next_token="t2")]
        out = self.run_cli(review_pages=pages, max_requests=2)
        self.assertEqual(out["collection_status"], "stopped_at_request_limit")
        self.assertEqual(len(self.api_calls), 2)
        self.assertFalse(out["review_coverage"]["all_reported_reviews_collected"])

    def test_no_main_photo_puts_review_photos_first(self):
        out = self.run_cli(place=place_response(thumbnail=None, images=[]),
                           review_pages=[reviews_page([review("r1", [REVIEW_URL_1, REVIEW_URL_2])])])
        saved = self.saved_output()
        self.assertIsNone(saved["main_restaurant_photo"])
        self.assertEqual([g["position"] for g in out["image_gallery"]], [1, 2])
        self.assertTrue(all(g["image_type"] == "customer_review_photo" for g in out["image_gallery"]))
        self.assertFalse(saved["image_summary"]["main_photo_downloaded"])
        self.assertIn("Main listing photo downloaded : No", self.stdout)

    def test_truncated_response_not_reported_as_full_history(self):
        page = reviews_page([review("r1", [])])
        del page["serpapi_pagination"]
        out = self.run_cli(review_pages=[page])
        coverage = out["review_coverage"]
        self.assertEqual(out["collection_status"], "completed")
        self.assertEqual(coverage["reviews_reported_by_google"], 3193)
        self.assertFalse(coverage["all_reported_reviews_collected"])
        self.assertIn("no 'serpapi_pagination' block", coverage["note"])
        self.assertIn("COVERAGE:", self.stdout)
        self.assertNotIn("all available pages retrieved", self.stdout)

    def test_fresh_sends_no_cache_on_review_requests(self):
        self.run_cli(review_pages=[reviews_page([review("r1", [])], next_token="t1"), reviews_page([])], fresh=True)
        review_calls = [c for c in self.api_calls if c["engine"] == "google_maps_reviews"]
        self.assertTrue(review_calls and all(c.get("no_cache") == "true" for c in review_calls))
        self.run_cli(review_pages=[reviews_page([])])
        self.assertNotIn("no_cache", self.api_calls[-1])

    def test_legacy_output_file_merged_and_kept(self):
        legacy = self.root / "data" / "output" / f"The_Food_Factory_{utils.safe_data_id(DATA_ID)}.json"
        utils.save_json({"data_id": DATA_ID, "reviews": [{"review_id": "old", "text": "older review", "images": []}]},
                        legacy)
        self.run_cli(review_pages=[reviews_page([review("new", [REVIEW_URL_1])])])
        saved = json.loads((self.root / "data" / "output" / f"{utils.safe_data_id(DATA_ID)}_reviews.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual([r["review_id"] for r in saved["reviews"]], ["new", "old"])
        self.assertTrue(legacy.exists())

    @staticmethod
    def place_with_user_reviews(*entries):
        place = place_response()
        place["place_results"]["user_reviews"] = {"most_relevant": [
            {"username": f"User {rid}", "rating": 5, "date": "4 months ago", "description": f"Review {rid}",
             "link": f"https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5!1s{rid}!2m1!1s0x0:0x1",
             "images": [{"thumbnail": u} for u in urls]}
            for rid, urls in entries]}
        return place

    def test_photos_from_place_details_reviews_collected_after_main_photo(self):
        place = self.place_with_user_reviews(("p1", [REVIEW_URL_1, REVIEW_URL_2]), ("p2", []))
        out = self.run_cli(place=place, review_pages=[reviews_page([review("r1", [])])])
        by_id = {r["review_id"]: r for r in out["reviews"]}
        self.assertEqual(set(by_id), {"r1", "p1", "p2"})
        self.assertEqual([i["image_url"] for i in by_id["p1"]["images"]], [REVIEW_URL_1, REVIEW_URL_2])
        self.assertEqual(by_id["p1"]["author"], "User p1")
        self.assertEqual(by_id["p1"]["text"], "Review p1")
        self.assertEqual([Path(i["local_path"]).name for i in by_id["p1"]["images"]],
                         ["review_p1_1.jpg", "review_p1_2.jpg"])
        self.assertEqual(self.session.calls[0], MAIN_URL)
        gallery = out["image_gallery"]
        self.assertEqual([g["image_type"] for g in gallery[:3]],
                         ["main_restaurant_photo", "customer_review_photo", "customer_review_photo"])
        self.assertEqual(out["image_summary"]["review_images_downloaded"], 2)

    def test_same_review_from_both_sources_merged_without_duplicates(self):
        resized = REVIEW_URL_1.replace("=w600", "=w1200")
        place = self.place_with_user_reviews(("r1", [resized, REVIEW_URL_2]))
        out = self.run_cli(place=place, review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        self.assertEqual(len(out["reviews"]), 1)
        self.assertEqual([i["image_url"] for i in out["reviews"][0]["images"]], [REVIEW_URL_1, REVIEW_URL_2])
        self.assertNotIn(resized, self.session.calls)

    def test_place_details_reviews_used_from_cache(self):
        place = self.place_with_user_reviews(("p1", [REVIEW_URL_1]))
        self.run_cli(place=place, review_pages=[reviews_page([review("r1", [])])])
        self.api_calls.clear()
        out = self.run_cli(from_cache=True)
        self.assertEqual(self.api_calls, [])
        self.assertIn("p1", {r["review_id"] for r in out["reviews"]})

    def test_review_photos_saved_under_old_names_are_reused(self):
        self.folder.mkdir(parents=True)
        (self.folder / "r1_1.jpg").write_bytes(JPEG + b"old download")
        rel = f"data/extracted_images/{utils.safe_data_id(DATA_ID)}/r1_1.jpg"
        utils.save_json({"by_url": {}, "by_slot": {"r1#1": rel}, "by_sha256": {}}, self.folder / "_manifest.json")
        out = self.run_cli(review_pages=[reviews_page([review("r1", [REVIEW_URL_1])])])
        img = out["reviews"][0]["images"][0]
        self.assertEqual((img["local_path"], img["download_status"]), (rel, "already_downloaded"))
        self.assertNotIn(REVIEW_URL_1, self.session.calls)
        self.assertFalse((self.folder / "review_r1_1.jpg").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
