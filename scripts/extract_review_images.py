"""
scripts/extract_review_images.py
================================
PITSTOP REVIEW IMAGE EXTRACTOR — main entry point.

Fetches Google Maps reviews for one restaurant (``data_id``) through
SerpApi's ``google_maps_reviews`` engine, extracts the images ATTACHED TO
EACH REVIEW (the review's ``images`` field — never general restaurant
photos), downloads them to ``data/extracted_images/<data_id>/`` and saves
one JSON file per restaurant in ``data/output/`` that links every image to
its review.

Usage (from the repository root, e.g. Windows PowerShell):

    python scripts/extract_review_images.py
    python scripts/extract_review_images.py --data-id 0xABCD:0x1234 --max-requests 3

Download rules:
  - Sequential downloads, short timeouts, limited retries on transient errors.
  - The response must be HTTP 200, declare an image Content-Type (when one is
    sent) AND start with known image magic bytes — HTML error pages are rejected.
  - The extension comes from the file's magic bytes, never from the remote URL.
  - Filenames: ``<review_id>_<n>.<ext>``; an existing different file is never
    overwritten (a ``_2``, ``_3`` ... suffix is used instead).
  - Identical images (same URL or same bytes) are stored once and referenced
    by every review that has them.
  - Failures are recorded per image in the JSON; they never stop the run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
    from scripts.utils import (
        EXTRACTED_IMAGES_DIR,
        load_json,
        make_review_fingerprint,
        safe_data_id,
        safe_name,
        save_json,
        short_hash,
        to_project_relative,
    )
except ImportError:
    import utils
    from utils import (
        EXTRACTED_IMAGES_DIR,
        load_json,
        make_review_fingerprint,
        safe_data_id,
        safe_name,
        save_json,
        short_hash,
        to_project_relative,
    )

# Seconds between individual image downloads (be polite to servers)
IMAGE_DOWNLOAD_DELAY: float = 0.3

# (connect, read) timeout for image downloads
IMAGE_TIMEOUT: tuple[int, int] = (5, 20)

# Retries for transient failures (timeouts, connection errors, 429, 5xx)
IMAGE_MAX_RETRIES: int = 2

# Reject anything larger than this (bytes)
MAX_IMAGE_BYTES: int = 25 * 1024 * 1024

MANIFEST_NAME: str = "_manifest.json"

USER_AGENT: str = "Pitstop-Review-Image-Extractor/1.0 (+python-requests)"

# Download statuses written to JSON
STATUS_DOWNLOADED = "downloaded"
STATUS_ALREADY = "already_downloaded"
STATUS_FAILED = "failed"
STATUS_INVALID_URL = "invalid_url"
STATUS_NOT_IMAGE = "not_an_image"
STATUS_SKIPPED_LIMIT = "skipped_limit"
STATUS_NOT_REQUESTED = "not_requested"


# ---------------------------------------------------------------------------
# Image type detection (magic bytes are the reliable source)
# ---------------------------------------------------------------------------

def detect_image_extension(head: bytes) -> Optional[str]:
    """Return a file extension from the leading bytes of an image, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return ".heic"
    if head.startswith(b"BM"):
        return ".bmp"
    return None


def _is_valid_image_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return detect_image_extension(fh.read(32)) is not None
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def review_file_stem(review: dict) -> str:
    """Safe filename stem for a review: sanitised review_id (+hash if truncated)."""
    rid = review.get("review_id")
    if rid:
        cleaned = safe_name(str(rid), 60)
        if cleaned != str(rid):
            cleaned = f"{cleaned[:50]}_{short_hash(str(rid), 8)}"
        return cleaned
    return f"noid_{short_hash(make_review_fingerprint(review), 12)}"


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

class ReviewImageDownloader:
    """
    Downloads review-attached images for one restaurant, sequentially.

    A manifest (``_manifest.json`` in the restaurant's image folder) remembers
    what was saved, so re-runs reuse files instead of downloading again.
    """

    def __init__(
        self,
        data_id: str,
        *,
        images_dir: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        delay: float = IMAGE_DOWNLOAD_DELAY,
        timeout: tuple[int, int] = IMAGE_TIMEOUT,
        max_retries: int = IMAGE_MAX_RETRIES,
        max_images: Optional[int] = None,
        max_bytes: int = MAX_IMAGE_BYTES,
        enabled: bool = True,
    ) -> None:
        # Read the module attribute at call time so the location can be redirected (tests)
        base = images_dir if images_dir is not None else utils.EXTRACTED_IMAGES_DIR
        self.folder = Path(base) / safe_data_id(data_id)
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_images = max_images
        self.max_bytes = max_bytes
        self.enabled = enabled

        self.download_attempts = 0
        self.downloaded = 0
        self._url_results: dict[str, dict] = {}
        self._manifest_path = self.folder / MANIFEST_NAME
        self._manifest: dict = {"by_url": {}, "by_slot": {}, "by_sha256": {}}
        self._load_manifest()

    # -- manifest ----------------------------------------------------------

    def _load_manifest(self) -> None:
        if not self._manifest_path.exists():
            return
        try:
            data = load_json(self._manifest_path)
        except (OSError, ValueError):
            return
        for key in self._manifest:
            if isinstance(data.get(key), dict):
                self._manifest[key] = data[key]

    def _save_manifest(self) -> None:
        try:
            save_json(self._manifest, self._manifest_path)
        except OSError as exc:
            print(f"  [WARN] Could not save image manifest: {exc}")

    def _existing_valid(self, rel_path: Optional[str]) -> Optional[Path]:
        if not rel_path:
            return None
        p = utils.PROJECT_ROOT / rel_path
        return p if p.exists() and _is_valid_image_file(p) else None

    def _remember(self, url: str, slot: str, rel_path: str, sha: str) -> None:
        self._manifest["by_url"][url] = rel_path
        self._manifest["by_slot"][slot] = rel_path
        self._manifest["by_sha256"][sha] = rel_path

    # -- public API --------------------------------------------------------

    def process_review(self, review: dict) -> dict:
        """
        Download every image in ``review["images"]`` and update each entry
        in place with ``local_path`` / ``download_status`` (and details).
        Returns the same review dict.
        """
        stem = review_file_stem(review)
        changed = False
        for entry in review.get("images") or []:
            if entry.get("download_status") in (STATUS_DOWNLOADED, STATUS_ALREADY) and self._existing_valid(entry.get("local_path")):
                continue
            entry.update(self._process_one(entry.get("image_url"), stem, int(entry.get("image_index") or 1)))
            changed = True
        if changed:
            self._save_manifest()
        return review

    # -- internals ---------------------------------------------------------

    def _process_one(self, url: Optional[str], stem: str, index: int) -> dict:
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return {"local_path": None, "download_status": STATUS_INVALID_URL, "error": "Missing or non-http(s) URL"}

        slot = f"{stem}#{index}"

        # Same URL already handled during this run (e.g. shared by two reviews)
        if url in self._url_results:
            prior = self._url_results[url]
            if prior.get("local_path"):
                return {**prior, "download_status": STATUS_ALREADY}
            return dict(prior)

        # Reuse files saved by an earlier run for this URL or this review slot
        for rel in (self._manifest["by_url"].get(url), self._manifest["by_slot"].get(slot)):
            if self._existing_valid(rel):
                result = {"local_path": rel, "download_status": STATUS_ALREADY}
                self._manifest["by_url"][url] = rel
                self._url_results[url] = result
                return dict(result)

        if not self.enabled:
            return {"local_path": None, "download_status": STATUS_NOT_REQUESTED}

        if self.max_images is not None and self.download_attempts >= self.max_images:
            return {"local_path": None, "download_status": STATUS_SKIPPED_LIMIT,
                    "error": f"Image download limit of {self.max_images} reached"}

        self.download_attempts += 1
        result = self._download(url, stem, index, slot)
        self._url_results[url] = result
        if self.delay > 0:
            time.sleep(self.delay)
        return dict(result)

    def _download(self, url: str, stem: str, index: int, slot: str) -> dict:
        last_error = "unknown error"
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
            except requests.exceptions.InvalidURL:
                return {"local_path": None, "download_status": STATUS_INVALID_URL, "error": "Malformed URL"}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_error = type(exc).__name__
                if attempt <= self.max_retries:
                    time.sleep(1.5 * attempt)
                    continue
                return {"local_path": None, "download_status": STATUS_FAILED, "error": last_error}
            except requests.exceptions.RequestException as exc:
                return {"local_path": None, "download_status": STATUS_FAILED, "error": type(exc).__name__}

            try:
                status = resp.status_code
                if status == 429 or status >= 500:
                    last_error = f"HTTP {status}"
                    if attempt <= self.max_retries:
                        time.sleep(2.0 * attempt)
                        continue
                    return {"local_path": None, "download_status": STATUS_FAILED, "http_status": status, "error": last_error}
                if status != 200:
                    return {"local_path": None, "download_status": STATUS_FAILED, "http_status": status, "error": f"HTTP {status}"}
                return self._save_response(resp, url, stem, index, slot)
            finally:
                resp.close()

        return {"local_path": None, "download_status": STATUS_FAILED, "error": last_error}

    def _save_response(self, resp: requests.Response, url: str, stem: str, index: int, slot: str) -> dict:
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith("image/"):
            return {"local_path": None, "download_status": STATUS_NOT_IMAGE, "http_status": 200,
                    "content_type": content_type, "error": "Response is not an image"}

        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return {"local_path": None, "download_status": STATUS_FAILED, "http_status": 200,
                    "content_type": content_type, "error": "Image exceeds size limit"}

        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f".{stem}_{index}.part"
        sha = hashlib.sha256()
        size = 0
        head = b""
        try:
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if len(head) < 32:
                        head += chunk[: 32 - len(head)]
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("Image exceeds size limit")
                    sha.update(chunk)
                    fh.write(chunk)
        except (requests.exceptions.RequestException, ValueError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            msg = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            return {"local_path": None, "download_status": STATUS_FAILED, "http_status": 200,
                    "content_type": content_type or None, "error": msg}

        ext = detect_image_extension(head)
        if size == 0 or ext is None:
            tmp.unlink(missing_ok=True)
            return {"local_path": None, "download_status": STATUS_NOT_IMAGE, "http_status": 200,
                    "content_type": content_type or None,
                    "error": "Empty response" if size == 0 else "Content is not a recognised image format"}

        digest = sha.hexdigest()
        meta = {"http_status": 200, "content_type": content_type or None, "file_size_bytes": size, "sha256": digest}

        # Identical bytes already stored (e.g. same photo under another URL)
        same = self._existing_valid(self._manifest["by_sha256"].get(digest))
        if same is not None:
            tmp.unlink(missing_ok=True)
            rel = to_project_relative(same)
            self._remember(url, slot, rel, digest)
            return {"local_path": rel, "download_status": STATUS_ALREADY, **meta}

        dest = self.folder / f"{stem}_{index}{ext}"
        n = 2
        while dest.exists():
            # Never overwrite: reuse only if it is byte-identical
            if _sha256_file(dest) == digest:
                tmp.unlink(missing_ok=True)
                rel = to_project_relative(dest)
                self._remember(url, slot, rel, digest)
                return {"local_path": rel, "download_status": STATUS_ALREADY, **meta}
            dest = self.folder / f"{stem}_{index}_{n}{ext}"
            n += 1

        os.replace(tmp, dest)
        rel = to_project_relative(dest)
        self._remember(url, slot, rel, digest)
        self.downloaded += 1
        return {"local_path": rel, "download_status": STATUS_DOWNLOADED, **meta}


# ---------------------------------------------------------------------------
# Backwards-compatible helper
# ---------------------------------------------------------------------------

def download_review_images(
    reviews: list[dict],
    data_id: str,
    *,
    delay: float = IMAGE_DOWNLOAD_DELAY,
    max_images: Optional[int] = None,
    images_dir: Optional[Path] = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """
    Download the images of already-flattened *reviews* (updated in place).

    Returns a summary ``{"downloaded", "already_downloaded", "failed", "saved_to"}``.
    """
    dl = ReviewImageDownloader(
        data_id, images_dir=images_dir, session=session, delay=delay, max_images=max_images,
    )
    for review in reviews:
        dl.process_review(review)
    statuses = [img.get("download_status") for r in reviews for img in r.get("images") or []]
    return {
        "downloaded":         statuses.count(STATUS_DOWNLOADED),
        "already_downloaded": statuses.count(STATUS_ALREADY),
        "failed":             len([s for s in statuses if s not in (STATUS_DOWNLOADED, STATUS_ALREADY)]),
        "saved_to":           to_project_relative(dl.folder),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract images attached to Google Maps reviews (SerpApi) and download them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/extract_review_images.py\n"
            "  python scripts/extract_review_images.py --data-id 0x3bae19cd83892495:0xc4ee26c182749dfe --max-requests 3\n"
            "  python scripts/extract_review_images.py --data-id 0x...:0x... --from-cache\n"
        ),
    )
    parser.add_argument("--data-id", metavar="DATA_ID", default=None,
                        help="Google Maps restaurant data_id (prompted if omitted).")
    parser.add_argument("--max-requests", type=int, default=utils.MAX_REQUESTS,
                        help=f"Max SerpApi search requests this run (1 request = 1 credit; default {utils.MAX_REQUESTS}).")
    parser.add_argument("--max-images", type=int, default=None,
                        help="Max image downloads this run (default: no limit).")
    parser.add_argument("--from-cache", action="store_true",
                        help="Rebuild from the last saved raw responses — uses 0 API credits.")
    parser.add_argument("--no-download", action="store_true",
                        help="Record review image URLs but do not download files.")
    parser.add_argument("--skip-key-check", action="store_true",
                        help="Skip the (free) SerpApi account check before searching.")
    args = parser.parse_args(argv)
    if args.max_requests < 1:
        parser.error("--max-requests must be at least 1")
    if args.max_images is not None and args.max_images < 0:
        parser.error("--max-images cannot be negative")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    try:
        from scripts import extract_reviews
    except ImportError:
        import extract_reviews

    args = parse_args(argv)
    print(f"\n{utils.DIVIDER}")
    print("   PITSTOP REVIEW IMAGE EXTRACTOR")
    print(f"{utils.DIVIDER}\n")

    data_id = args.data_id or utils.prompt_data_id()
    if not utils.is_valid_data_id(data_id):
        print("[ERROR] Invalid data_id format. Expected 0x...:0x...")
        return 1
    print()

    result = extract_reviews.run_cli(
        data_id,
        max_requests=args.max_requests,
        from_cache=args.from_cache,
        skip_key_check=args.skip_key_check,
        downloader=ReviewImageDownloader(data_id, max_images=args.max_images, enabled=not args.no_download),
    )
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
