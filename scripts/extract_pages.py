"""
scripts/extract_pages.py
========================
Handles saving, loading, and listing raw API page responses.

Raw pages are stored in ``data/extracted_pages/`` as JSON files named:
    <safe_data_id>_page<NNN>.json

This module is useful for:
  - Debugging: inspect exactly what SerpApi returned for each page.
  - Reproducibility: re-process existing pages without new API calls.
  - Resuming: detect which pages have already been saved.

Typical usage from the extraction loop (called by extract_reviews.py):

    from scripts.extract_pages import save_page, list_saved_pages, load_page

    save_page(data_id="0xABCD:0x1234", page_num=1, page_data=api_response)
    paths = list_saved_pages("0xABCD:0x1234")
    reloaded = load_page(paths[0])
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts.utils import EXTRACTED_PAGES_DIR, save_json
except ImportError:
    from utils import EXTRACTED_PAGES_DIR, save_json


def _safe_id(data_id: str, max_len: int = 40) -> str:
    """Return a filesystem-safe version of *data_id*."""
    return (
        data_id.replace(":", "_").replace("/", "_").replace("\\", "_")[:max_len]
    )


def save_page(data_id: str, page_num: int, page_data: dict) -> Path:
    """
    Persist a raw API page response to ``data/extracted_pages/``.

    Args:
        data_id:   The restaurant's Google Maps data_id.
        page_num:  1-based page number.
        page_data: The parsed JSON dict returned by SerpApi.

    Returns:
        The :class:`pathlib.Path` of the saved file.
    """
    EXTRACTED_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_id(data_id)}_page{page_num:03d}.json"
    path = EXTRACTED_PAGES_DIR / filename
    save_json(page_data, path)
    return path


def load_page(path: Path | str) -> dict:
    """
    Load a previously saved raw page response.

    Args:
        path: Absolute or relative path to a page JSON file.

    Returns:
        Parsed dict from the file.

    Raises:
        FileNotFoundError if the file does not exist.
        ValueError if the file cannot be parsed as JSON.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Page file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cannot parse JSON from {path}: {exc}") from exc


def list_saved_pages(data_id: str) -> list[Path]:
    """
    Return a sorted list of all saved page files for a given *data_id*.

    Args:
        data_id: The restaurant's Google Maps data_id.

    Returns:
        Sorted list of :class:`pathlib.Path` objects (may be empty).
    """
    prefix = _safe_id(data_id)
    if not EXTRACTED_PAGES_DIR.exists():
        return []
    return sorted(EXTRACTED_PAGES_DIR.glob(f"{prefix}_page*.json"))


def iter_saved_pages(data_id: str) -> Iterator[dict]:
    """
    Yield parsed dicts for all saved pages of *data_id* in order.

    Useful for reprocessing existing pages without new API calls.
    """
    for path in list_saved_pages(data_id):
        yield load_page(path)


def count_saved_pages(data_id: str) -> int:
    """Return the number of saved page files for *data_id*."""
    return len(list_saved_pages(data_id))
