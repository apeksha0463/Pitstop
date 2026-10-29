"""
scripts/tests/test_extract_review_images.py
===========================================
Tests for scripts/extract_review_images.py — downloading images attached
to reviews.

Covers:
  - Reviews with no images / one image / multiple images
  - Successful downloads, extension from magic bytes (not the remote URL)
  - HTML error pages and non-image bodies are rejected
  - HTTP errors, timeouts, retries and invalid URLs
  - Duplicate images (same URL, same bytes) stored once, still linked to every review
  - Existing files are never overwritten; re-runs reuse files via the manifest
  - Download / size limits, safe filenames

No real network access: a fake requests session is injected.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import extract_review_images as eri
    from scripts.utils import flatten_review
except ImportError:
    import extract_review_images as eri
    from utils import flatten_review

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x01" * 200
JPEG_2 = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x02" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 200
HTML = b"<!DOCTYPE html><html><body>Error 404</body></html>"


class FakeResponse:
    def __init__(self, status=200, body=b"", content_type="image/jpeg", headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.headers.update(headers or {})
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Maps URL -> FakeResponse, list of responses (consumed in order) or exception."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        route = self.routes.get(url)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        if route is None:
            return FakeResponse(404, b"", "text/html")
        return route


def make_review(review_id, urls):
    return flatten_review({"review_id": review_id, "rating": 5, "snippet": "ok", "images": urls})


class _TempProject(unittest.TestCase):
    """Runs each test inside a throw-away project root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.images_dir = self.root / "data" / "extracted_images"
        self._patch = patch.object(eri.utils, "PROJECT_ROOT", self.root)
        self._patch.start()
        self._sleep = patch.object(eri.time, "sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def downloader(self, routes, **kw):
        self.session = FakeSession(routes)
        return eri.ReviewImageDownloader("0xAB:0xCD", images_dir=self.images_dir,
                                         session=self.session, delay=0, **kw)

    @property
    def folder(self):
        return self.images_dir / "0xAB_0xCD"


class TestBasicDownloads(_TempProject):

    def test_review_without_images_makes_no_requests(self):
        dl = self.downloader({})
        review = dl.process_review(make_review("r0", []))
        self.assertEqual(review["images"], [])
        self.assertEqual(self.session.calls, [])

    def test_single_image_downloaded_and_linked(self):
        dl = self.downloader({"https://img.test/a": FakeResponse(body=JPEG)})
        review = dl.process_review(make_review("rev1", ["https://img.test/a"]))
        img = review["images"][0]
        self.assertEqual(img["download_status"], "downloaded")
        self.assertEqual(img["image_url"], "https://img.test/a")
        self.assertEqual(img["local_path"], "data/extracted_images/0xAB_0xCD/review_rev1_1.jpg")
        self.assertEqual((self.root / img["local_path"]).read_bytes(), JPEG)
        self.assertEqual(img["file_size_bytes"], len(JPEG))

    def test_multiple_images_get_indexed_names_and_real_extensions(self):
        dl = self.downloader({
            "https://img.test/1.jpg": FakeResponse(body=PNG, content_type="image/jpeg"),
            "https://img.test/2": FakeResponse(body=WEBP, content_type="image/webp"),
            "https://img.test/3": FakeResponse(body=JPEG, content_type=None),
        })
        review = dl.process_review(make_review("rev2", ["https://img.test/1.jpg", "https://img.test/2", "https://img.test/3"]))
        paths = [Path(i["local_path"]).name for i in review["images"]]
        self.assertEqual(paths, ["review_rev2_1.png", "review_rev2_2.webp", "review_rev2_3.jpg"])
        self.assertTrue(all(i["download_status"] == "downloaded" for i in review["images"]))

    def test_remote_filename_is_not_trusted(self):
        dl = self.downloader({"https://img.test/payload.exe": FakeResponse(body=JPEG)})
        review = dl.process_review(make_review("rev3", ["https://img.test/payload.exe"]))
        self.assertTrue(review["images"][0]["local_path"].endswith("review_rev3_1.jpg"))

    def test_no_partial_files_left_behind(self):
        dl = self.downloader({"https://img.test/a": FakeResponse(body=JPEG),
                              "https://img.test/b": FakeResponse(body=HTML, content_type="image/jpeg")})
        dl.process_review(make_review("rev4", ["https://img.test/a", "https://img.test/b"]))
        self.assertEqual(list(self.folder.glob("*.part")) + list(self.folder.glob(".*.part")), [])


class TestValidationAndFailures(_TempProject):

    def test_html_error_page_rejected_by_content_type(self):
        dl = self.downloader({"https://img.test/x": FakeResponse(body=HTML, content_type="text/html")})
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "not_an_image")
        self.assertIsNone(img["local_path"])
        self.assertFalse(self.folder.exists() and any(self.folder.glob("r_*")))

    def test_html_body_with_image_content_type_rejected(self):
        dl = self.downloader({"https://img.test/x": FakeResponse(body=HTML, content_type="image/jpeg")})
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "not_an_image")
        self.assertFalse(any(self.folder.glob("r_*")))

    def test_empty_body_rejected(self):
        dl = self.downloader({"https://img.test/x": FakeResponse(body=b"")})
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "not_an_image")

    def test_http_404_recorded_without_retry(self):
        dl = self.downloader({"https://img.test/x": FakeResponse(404, b"", "text/html")})
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "failed")
        self.assertEqual(img["http_status"], 404)
        self.assertEqual(len(self.session.calls), 1)

    def test_server_error_retried_then_succeeds(self):
        dl = self.downloader({"https://img.test/x": [FakeResponse(503, b"", "text/html"), FakeResponse(body=JPEG)]})
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "downloaded")
        self.assertEqual(len(self.session.calls), 2)

    def test_timeouts_limited_retries_then_failed(self):
        dl = self.downloader({"https://img.test/x": requests.exceptions.Timeout()}, max_retries=2)
        img = dl.process_review(make_review("r", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "failed")
        self.assertEqual(img["error"], "Timeout")
        self.assertEqual(len(self.session.calls), 3)

    def test_invalid_urls_not_requested(self):
        dl = self.downloader({})
        review = make_review("r", [])
        review["images"] = [{"image_url": None, "image_index": 1},
                            {"image_url": "javascript:alert(1)", "image_index": 2}]
        dl.process_review(review)
        self.assertEqual([i["download_status"] for i in review["images"]], ["invalid_url", "invalid_url"])
        self.assertEqual(self.session.calls, [])

    def test_malformed_url_reported(self):
        dl = self.downloader({"https://bad host/x": requests.exceptions.InvalidURL()})
        img = dl.process_review(make_review("r", ["https://bad host/x"]))["images"][0]
        self.assertEqual(img["download_status"], "invalid_url")

    def test_one_failure_does_not_stop_other_images(self):
        dl = self.downloader({"https://img.test/bad": requests.exceptions.ConnectionError(),
                              "https://img.test/good": FakeResponse(body=JPEG)}, max_retries=0)
        review = dl.process_review(make_review("r", ["https://img.test/bad", "https://img.test/good"]))
        self.assertEqual([i["download_status"] for i in review["images"]], ["failed", "downloaded"])

    def test_oversized_image_rejected(self):
        dl = self.downloader({"https://img.test/big": FakeResponse(body=JPEG * 10)}, max_bytes=500)
        img = dl.process_review(make_review("r", ["https://img.test/big"]))["images"][0]
        self.assertEqual(img["download_status"], "failed")
        self.assertIn("size limit", img["error"])
        self.assertFalse(any(self.folder.glob("r_*")))

    def test_max_images_limit(self):
        dl = self.downloader({"https://img.test/1": FakeResponse(body=JPEG),
                              "https://img.test/2": FakeResponse(body=JPEG_2)}, max_images=1)
        review = dl.process_review(make_review("r", ["https://img.test/1", "https://img.test/2"]))
        self.assertEqual([i["download_status"] for i in review["images"]], ["downloaded", "skipped_limit"])

    def test_disabled_downloader_records_urls_only(self):
        dl = self.downloader({"https://img.test/1": FakeResponse(body=JPEG)}, enabled=False)
        img = dl.process_review(make_review("r", ["https://img.test/1"]))["images"][0]
        self.assertEqual(img["download_status"], "not_requested")
        self.assertEqual(img["image_url"], "https://img.test/1")
        self.assertEqual(self.session.calls, [])


class TestDeduplicationAndOverwrite(_TempProject):

    def test_same_url_in_two_reviews_downloaded_once(self):
        dl = self.downloader({"https://img.test/shared": FakeResponse(body=JPEG)})
        r1 = dl.process_review(make_review("rA", ["https://img.test/shared"]))
        r2 = dl.process_review(make_review("rB", ["https://img.test/shared"]))
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(r1["images"][0]["download_status"], "downloaded")
        self.assertEqual(r2["images"][0]["download_status"], "already_downloaded")
        self.assertEqual(r1["images"][0]["local_path"], r2["images"][0]["local_path"])

    def test_identical_bytes_from_different_urls_stored_once(self):
        dl = self.downloader({"https://img.test/u1": FakeResponse(body=JPEG),
                              "https://img.test/u2": FakeResponse(body=JPEG)})
        r1 = dl.process_review(make_review("rA", ["https://img.test/u1"]))
        r2 = dl.process_review(make_review("rB", ["https://img.test/u2"]))
        self.assertEqual(r2["images"][0]["download_status"], "already_downloaded")
        self.assertEqual(r1["images"][0]["local_path"], r2["images"][0]["local_path"])
        self.assertEqual(len([p for p in self.folder.iterdir() if p.suffix == ".jpg"]), 1)

    def test_existing_different_file_is_not_overwritten(self):
        self.folder.mkdir(parents=True)
        existing = self.folder / "review_rev1_1.jpg"
        existing.write_bytes(JPEG_2)
        dl = self.downloader({"https://img.test/a": FakeResponse(body=JPEG)})
        img = dl.process_review(make_review("rev1", ["https://img.test/a"]))["images"][0]
        self.assertEqual(existing.read_bytes(), JPEG_2, "existing file must be preserved")
        self.assertTrue(img["local_path"].endswith("review_rev1_1_2.jpg"))
        self.assertEqual((self.root / img["local_path"]).read_bytes(), JPEG)

    def test_existing_identical_file_is_reused(self):
        self.folder.mkdir(parents=True)
        (self.folder / "review_rev1_1.jpg").write_bytes(JPEG)
        dl = self.downloader({"https://img.test/a": FakeResponse(body=JPEG)})
        img = dl.process_review(make_review("rev1", ["https://img.test/a"]))["images"][0]
        self.assertEqual(img["download_status"], "already_downloaded")
        self.assertTrue(img["local_path"].endswith("review_rev1_1.jpg"))
        self.assertEqual(len(list(self.folder.glob("review_rev1_*"))), 1)

    def test_rerun_reuses_manifest_without_new_requests(self):
        first = self.downloader({"https://img.test/a": FakeResponse(body=JPEG)})
        first.process_review(make_review("rev1", ["https://img.test/a"]))
        # New run: Google often returns a different signed URL for the same review image
        second = self.downloader({})
        img = second.process_review(make_review("rev1", ["https://img.test/a-new-token"]))["images"][0]
        self.assertEqual(img["download_status"], "already_downloaded")
        self.assertEqual(self.session.calls, [])

    def test_failed_url_not_retried_within_same_run(self):
        dl = self.downloader({"https://img.test/x": FakeResponse(404, b"", "text/html")})
        dl.process_review(make_review("rA", ["https://img.test/x"]))
        img = dl.process_review(make_review("rB", ["https://img.test/x"]))["images"][0]
        self.assertEqual(img["download_status"], "failed")
        self.assertEqual(len(self.session.calls), 1)


class TestFilenames(unittest.TestCase):

    def test_unsafe_review_id_sanitised(self):
        stem = eri.review_file_stem({"review_id": "../../etc/passwd==:?*"})
        self.assertRegex(stem, r"^[A-Za-z0-9_-]+$")
        self.assertNotIn("..", stem)

    def test_distinct_ids_do_not_collide_after_sanitising(self):
        a = eri.review_file_stem({"review_id": "abc/def"})
        b = eri.review_file_stem({"review_id": "abc:def"})
        self.assertNotEqual(a, b)

    def test_long_review_id_truncated(self):
        self.assertLessEqual(len(eri.review_file_stem({"review_id": "A" * 300})), 60)

    def test_review_without_id_gets_stable_stem(self):
        review = {"author": "Alice", "date": "1 week ago", "text": "Nice"}
        self.assertEqual(eri.review_file_stem(review), eri.review_file_stem(dict(review)))
        self.assertTrue(eri.review_file_stem(review).startswith("noid_"))


class TestDetectImageExtension(unittest.TestCase):

    def test_known_formats(self):
        self.assertEqual(eri.detect_image_extension(JPEG), ".jpg")
        self.assertEqual(eri.detect_image_extension(PNG), ".png")
        self.assertEqual(eri.detect_image_extension(WEBP), ".webp")
        self.assertEqual(eri.detect_image_extension(b"GIF89a" + b"\x00" * 10), ".gif")
        self.assertEqual(eri.detect_image_extension(b"\x00\x00\x00\x1cftypavif"), ".avif")

    def test_rejects_html_and_garbage(self):
        self.assertIsNone(eri.detect_image_extension(HTML))
        self.assertIsNone(eri.detect_image_extension(b""))
        self.assertIsNone(eri.detect_image_extension(b"MZ\x90\x00"))


class TestDownloadReviewImagesHelper(_TempProject):

    def test_summary_counts(self):
        reviews = [make_review("a", ["https://img.test/1"]), make_review("b", ["https://img.test/missing"])]
        session = FakeSession({"https://img.test/1": FakeResponse(body=JPEG)})
        summary = eri.download_review_images(reviews, "0xAB:0xCD", delay=0,
                                             images_dir=self.images_dir, session=session)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(reviews[0]["images"][0]["download_status"], "downloaded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
