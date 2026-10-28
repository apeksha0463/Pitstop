"""
scripts/tests/test_extract_images.py
====================================
Tests for scripts/extract_images.py — review image downloading.

Covers:
  - download_review_images returns correct summary dict
  - Skips None / empty URLs
  - Skips already-downloaded files
  - Handles download errors gracefully (no crash)
  - max_images cap is respected
  - Extension detection from URL and Content-Type
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent      # scripts/tests/
_SCRIPTS = _HERE.parent                      # scripts/
_ROOT = _SCRIPTS.parent                      # repository root
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import scripts.extract_images as ei
except ImportError:
    import extract_images as ei


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_review(review_id: str, photo_urls: list) -> dict:
    return {"review_id": review_id, "photos": photo_urls}


def _fake_response(content: bytes = b"FAKEIMAGE", status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.iter_content = lambda chunk_size: [content]
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDownloadReviewImages(unittest.TestCase):

    def test_downloads_single_image(self):
        reviews = [_make_review("r1", ["https://example.com/photo1.jpg"])]
        fake_resp = _fake_response()

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with (
                patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir),
                patch.object(ei.requests, "get", return_value=fake_resp),
            ):
                report = ei.download_review_images(reviews, "0xABCD:0x1234", delay=0)

        self.assertEqual(report["downloaded"], 1)
        self.assertEqual(report["failed"], 0)

    def test_skips_none_urls(self):
        reviews = [_make_review("r1", [None, "", None])]

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir):
                report = ei.download_review_images(reviews, "0xABCD:0x1234", delay=0)

        self.assertEqual(report["downloaded"], 0)
        self.assertEqual(report["skipped"], 3)

    def test_skips_already_downloaded(self):
        reviews = [_make_review("r1", ["https://example.com/photo1.jpg"])]
        fake_resp = _fake_response()

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with (
                patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir),
                patch.object(ei.requests, "get", return_value=fake_resp),
            ):
                # First download
                ei.download_review_images(reviews, "0xABCD:0x1234", delay=0)
                # Second download — should skip
                report = ei.download_review_images(reviews, "0xABCD:0x1234", delay=0)

        self.assertEqual(report["downloaded"], 0)
        self.assertEqual(report["skipped"], 1)

    def test_handles_download_error_gracefully(self):
        reviews = [_make_review("r1", ["https://example.com/photo_bad.jpg"])]

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with (
                patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir),
                patch.object(
                    ei.requests,
                    "get",
                    side_effect=Exception("Connection refused"),
                ),
            ):
                # Should NOT raise — just log and count as failed
                report = ei.download_review_images(reviews, "0xABCD:0x1234", delay=0)

        self.assertEqual(report["downloaded"], 0)
        self.assertEqual(report["failed"], 1)

    def test_max_images_cap(self):
        reviews = [
            _make_review("r1", [
                "https://example.com/p1.jpg",
                "https://example.com/p2.jpg",
                "https://example.com/p3.jpg",
            ])
        ]
        fake_resp = _fake_response()

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with (
                patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir),
                patch.object(ei.requests, "get", return_value=fake_resp),
            ):
                report = ei.download_review_images(
                    reviews, "0xABCD:0x1234", delay=0, max_images=2
                )

        self.assertEqual(report["downloaded"], 2)

    def test_empty_reviews_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_dir = Path(tmpdir)
            with patch.object(ei, "EXTRACTED_IMAGES_DIR", fake_dir):
                report = ei.download_review_images([], "0xABCD:0x1234", delay=0)

        self.assertEqual(report["downloaded"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["failed"], 0)


class TestExtensionDetection(unittest.TestCase):

    def test_ext_from_jpg_url(self):
        ext = ei._ext_from_url("https://example.com/image.jpg")
        self.assertEqual(ext, ".jpg")

    def test_ext_from_png_url(self):
        ext = ei._ext_from_url("https://example.com/photo.png")
        self.assertEqual(ext, ".png")

    def test_ext_fallback_for_unknown_url(self):
        ext = ei._ext_from_url("https://example.com/image")
        self.assertEqual(ext, ei.FALLBACK_EXTENSION)

    def test_ext_from_jpeg_content_type(self):
        ext = ei._ext_from_content_type("image/jpeg")
        self.assertEqual(ext, ".jpg")

    def test_ext_from_none_content_type(self):
        ext = ei._ext_from_content_type(None)
        self.assertEqual(ext, ei.FALLBACK_EXTENSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
