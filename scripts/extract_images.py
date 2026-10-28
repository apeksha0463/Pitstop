"""
scripts/extract_images.py
=========================
Extracts and downloads review-related images when image URLs are
available and downloading is permitted.

Review photo URLs are collected from the ``photos`` field of each
flattened review (populated by ``scripts.utils.flatten_review``).
Downloaded files are stored in ``data/extracted_images/``.

Usage:

    from scripts.extract_images import download_review_images

    # Pass the list of flattened reviews from extract_reviews.py
    report = download_review_images(reviews, data_id="0xABCD:0x1234")
    print(report)

Notes:
  - Requires ``requests`` (already in requirements.txt).
  - Skips URLs that are None, empty, or already downloaded.
  - Saves files as ``<safe_data_id>_<review_id>_<index>.<ext>``.
  - Never raises on individual download failures; logs warnings instead.
  - Respects a configurable delay between downloads.
"""

from __future__ import annotations

import mimetypes
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts.utils import EXTRACTED_IMAGES_DIR
except ImportError:
    from utils import EXTRACTED_IMAGES_DIR

# Seconds between individual image downloads (be polite to servers)
IMAGE_DOWNLOAD_DELAY: float = 0.5

# HTTP timeout for image downloads
IMAGE_TIMEOUT: int = 20

# Extension to use when we cannot determine one from the URL / Content-Type
FALLBACK_EXTENSION: str = ".jpg"


def _safe_id(data_id: str, max_len: int = 30) -> str:
    """Return a filesystem-safe version of *data_id*."""
    return data_id.replace(":", "_").replace("/", "_").replace("\\", "_")[:max_len]


def _ext_from_url(url: str) -> str:
    """
    Guess a file extension from a URL path.

    Falls back to ``FALLBACK_EXTENSION`` if none can be determined.
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    _, ext = Path(path).suffix.split("?")[0] if "?" in Path(path).suffix else (None, Path(path).suffix)
    if ext and ext.startswith(".") and len(ext) <= 5:
        return ext.lower()
    return FALLBACK_EXTENSION


def _ext_from_content_type(content_type: Optional[str]) -> str:
    """
    Guess a file extension from a ``Content-Type`` header value.

    Falls back to ``FALLBACK_EXTENSION`` if none can be determined.
    """
    if not content_type:
        return FALLBACK_EXTENSION
    mime = content_type.split(";")[0].strip()
    ext = mimetypes.guess_extension(mime)
    if ext:
        # Python sometimes returns '.jpe' for JPEG; normalise
        return ".jpg" if ext in (".jpe", ".jpeg") else ext
    return FALLBACK_EXTENSION


def download_review_images(
    reviews: list[dict],
    data_id: str,
    *,
    delay: float = IMAGE_DOWNLOAD_DELAY,
    timeout: int = IMAGE_TIMEOUT,
    max_images: Optional[int] = None,
) -> dict:
    """
    Download all photo URLs found in *reviews* to ``data/extracted_images/``.

    Args:
        reviews:    List of flattened review dicts (from ``flatten_review``).
        data_id:    The restaurant's Google Maps data_id (used for filenames).
        delay:      Seconds to wait between downloads.
        timeout:    HTTP timeout per image request.
        max_images: Optional cap on the total number of images to download.

    Returns:
        A summary dict::

            {
                "downloaded": int,
                "skipped":    int,
                "failed":     int,
                "saved_to":   str,   # str(EXTRACTED_IMAGES_DIR)
            }
    """
    EXTRACTED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    safe = _safe_id(data_id)

    downloaded = 0
    skipped = 0
    failed = 0
    total_processed = 0

    for review in reviews:
        review_id = review.get("review_id") or "noid"
        photos: list = review.get("photos") or []

        for idx, url in enumerate(photos):
            if not url or not isinstance(url, str):
                skipped += 1
                continue

            if max_images is not None and downloaded >= max_images:
                skipped += len(photos) - idx
                break

            # Determine destination filename
            ext = _ext_from_url(url)
            filename = f"{safe}_{review_id}_{idx:02d}{ext}"
            dest = EXTRACTED_IMAGES_DIR / filename

            if dest.exists():
                skipped += 1
                continue

            # Download
            try:
                resp = requests.get(url, timeout=timeout, stream=True)
                resp.raise_for_status()

                # Refine extension from actual Content-Type
                ct = resp.headers.get("Content-Type")
                real_ext = _ext_from_content_type(ct)
                if real_ext != ext:
                    dest = EXTRACTED_IMAGES_DIR / f"{safe}_{review_id}_{idx:02d}{real_ext}"

                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)

                downloaded += 1
                total_processed += 1

                if delay > 0:
                    time.sleep(delay)

            except Exception as exc:
                print(f"  [WARN] Failed to download image {url!r}: {exc}")
                failed += 1

    return {
        "downloaded": downloaded,
        "skipped":    skipped,
        "failed":     failed,
        "saved_to":   str(EXTRACTED_IMAGES_DIR),
    }
