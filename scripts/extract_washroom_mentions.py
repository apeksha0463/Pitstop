"""
scripts/extract_washroom_mentions.py
====================================
Find the washroom-related reviews among every review collected so far and
pull out just the sentence(s) about the washroom (0 API credits).

A review is washroom-related when its text mentions washroom, restroom,
toilet, bathroom, loo, lavatory, wash room, rest room, WC, urinal or a hand
wash area (case-insensitive, whole words, common misspellings included).
Only the washroom sentences are kept as ``washroom_snippet``, so the rest of
the review (usually about the food) cannot skew a later sentiment score.
Each washroom-related review is one row (and counts once), even when it talks
about the washroom in several places: those passages are joined with " … ".

Reads:
  - data/raw_data/restaurants_route_above_3.5.csv   restaurant list (data_id)
  - every collected review, merged first by merge_reviews.py into
    data/processed/reviews/ and data/processed/all_reviews.csv (SerpApi
    unfiltered, SerpApi washroom searches and Apify, each review once)
Writes:
  - data/processed/washroom_mentions.csv  one row per washroom-related review
  - data/processed/washroom_summary.csv   one row per restaurant

Usage (from the repository root):

    python scripts/extract_washroom_mentions.py
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import merge_reviews, utils
    from scripts.utils import load_json, make_review_fingerprint, short_hash, to_project_relative
except ImportError:
    import merge_reviews
    import utils
    from utils import load_json, make_review_fingerprint, short_hash, to_project_relative

DEFAULT_CSV: Path = utils.RAW_DATA_DIR / "restaurants_route_above_3.5.csv"

MENTION_COLUMNS = [
    "mention_id", "restaurant", "data_id", "review_id", "review_date", "iso_date",
    "star_rating", "washroom_snippet", "passage_count", "full_review_text", "review_link",
]
SUMMARY_COLUMNS = [
    "restaurant", "data_id", "google_review_count", "sample_reviews", "sample_reviews_with_text",
    "washroom_in_sample", "washroom_share", "washroom_search_reviews", "washroom_related_reviews",
    "latest_mention_date",
]
# The share is measured on each restaurant's newest N unfiltered reviews, the same
# definition for every restaurant (older downloads in Google's "most relevant"
# order favour long reviews, which mention washrooms far more often)
DEFAULT_SAMPLE_SIZE = 100
PASSAGE_JOINER = " … "

# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

_SEP = r"[\s-]?"   # "wash room", "wash-room", "washroom"
_TERMS = (
    rf"wash{_SEP}r+o{{1,3}}[mn]s?",          # washroom, wash room, washrom, washroon, washrrom
    r"wahsr+o{1,3}ms?|wasr+o{2,3}ms?",      # wahsroom, wasroom (never "was room")
    rf"rest{_SEP}r+o{{1,3}}ms?|resr+o{{2}}ms?",  # restroom, rest room, restrom, resroom
    r"toilett?e?s?|toliets?|tiolets?|toillets?|toilts?|toylets?",  # toilet(s), toliet, toilette (not "tolet")
    rf"bath{_SEP}r+o{{1,3}}[mn]s?|batr+o{{2}}ms?",  # bathroom, bath room, bathrom, batroom
    r"loos?",
    r"lava?t[oa]?r(?:y|ies)|lavotor(?:y|ies)",  # lavatory, lavatry, lavatary, lavtory
    r"w\.?c",                               # WC, W.C.
    r"urinals?",
    rf"hand{_SEP}wash(?:ing)?{_SEP}(?:area|station|room|basin)s?",
)
# "bathroom-themed decor" or "toilet humour" use the word without being about the facilities
_NOT_ABOUT_FACILITIES = r"(?!-(?:themed?|inspired|styled?|shaped)\b)(?!\s+(?:humou?r|jokes?)\b)"

WASHROOM_RE = re.compile(r"\b(?:" + "|".join(_TERMS) + r")\b" + _NOT_ABOUT_FACILITIES, re.IGNORECASE)


def find_washroom_terms(text: Optional[str]) -> list[str]:
    """The washroom words found in *text*, as written (empty when unrelated)."""
    return [m.group(0) for m in WASHROOM_RE.finditer(text or "")]


def is_washroom_related(text: Optional[str]) -> bool:
    return bool(WASHROOM_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------

# Sentence ends: . ! ? … (also runs like "!!!", "..." or "…."), also before a closing
# bracket (".) Next"), dot runs or "…" with no space after them ("food was
# cold...washroom dirty"), a stop glued to the next capitalised word ("taste .Dosa
# was raw", but not initials like "W.C") and line breaks
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?…])\s+|(?<=[.!?…]\))\s+|(?<=[.…]{2})(?=\w)|(?<=…)(?=\w)|(?<=[^A-Z][.!?])(?=[A-Z](?!\.))|\s*\n+\s*"
)
# A short washroom sentence that only introduces what follows ("Parking/Restroom:", "And the restrooms?")
_LEAD_IN = re.compile(r"[:?]$")
MAX_LEAD_IN = 40
# Clause breaks used to trim a long sentence that also covers other things
_CLAUSE_SPLIT = re.compile(r"\s*[,;:]\s+|\s+(?=(?:but|however|though|whereas)\b)", re.IGNORECASE)
# A short follow-on sentence that carries on about the washroom ("Washroom is upstairs. It was dirty.")
_CONTINUATION = re.compile(
    r"^(?:it|they|these|those)\s+(?:is|was|are|were|had|has|have|smell(?:s|ed|y)?|stinks?|needs?|"
    r"could|should|can|did|didn'?t|isn'?t|wasn'?t|aren'?t|weren'?t|lacks?|looked|looks?)\b",
    re.IGNORECASE,
)
# A relative clause that describes the washroom ("the bathroom, which was not clean")
_RELATIVE = re.compile(r"^(?:which|that|where)\b", re.IGNORECASE)
LONG_SENTENCE = 100
MIN_CLAUSE = 40
MAX_CONTINUATION = 120


def clean_review_text(text: Optional[str]) -> str:
    """Review text without Google's translation wrapper (keeps the English part)."""
    text = text or ""
    text = re.sub(r"^\s*\(Translated by Google\)\s*", "", text)
    return re.split(r"\s*\(Original\)\s*", text, maxsplit=1)[0]


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _trim_long_sentence(sentence: str) -> str:
    """For a long sentence keep only the clauses about the washroom."""
    clauses = [c for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip()]
    keep: list[str] = []
    for i, clause in enumerate(clauses):
        if not is_washroom_related(clause):
            continue
        piece = [clause]
        j = i + 1
        # A bare "the washroom" clause says nothing on its own: add what follows
        while j < len(clauses) and (len(" ".join(piece)) < MIN_CLAUSE or _RELATIVE.match(clauses[j])):
            piece.append(clauses[j])
            j += 1
        joined = _plain(" ".join(piece))
        if not any(joined in k for k in keep):
            keep.append(joined)
    return ", ".join(keep) if keep else sentence


def extract_washroom_snippets(text: Optional[str]) -> list[str]:
    """
    The washroom passages of one review, one string per mention.

    Consecutive washroom sentences (plus a short follow-on sentence such as
    "It was dirty.") form one mention; separate passages are separate
    mentions. Returns plain text (no line breaks), or [] when unrelated.
    """
    sentences = [s for s in (_plain(x) for x in _SENTENCE_SPLIT.split(clean_review_text(text))) if s]
    mentions: list[list[str]] = []
    current: list[str] = []
    for sentence in sentences:
        if is_washroom_related(sentence):
            current.append(_trim_long_sentence(sentence) if len(sentence) > LONG_SENTENCE else sentence)
        elif (current and _LEAD_IN.search(current[-1]) and len(current[-1]) <= MAX_LEAD_IN
              and len(sentence) <= MAX_CONTINUATION):
            current.append(sentence)
        elif current and _CONTINUATION.match(sentence) and len(sentence) <= MAX_CONTINUATION:
            current.append(sentence)
        elif current:
            mentions.append(current)
            current = []
    if current:
        mentions.append(current)
    return [" ".join(m) for m in mentions]


# ---------------------------------------------------------------------------
# Loading collected reviews
# ---------------------------------------------------------------------------

def load_restaurants(csv_path: Path = DEFAULT_CSV) -> list[dict]:
    """``[{"restaurant", "data_id", "google_review_count"}]`` from the restaurant list, in file order."""
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = [{"restaurant": (r.get("Restaurant") or "").strip(), "data_id": (r.get("data_id") or "").strip(),
                 "google_review_count": (r.get("No. of reviews") or "").replace(",", "").strip()}
                for r in csv.DictReader(fh)]
    seen: set[str] = set()
    restaurants = []
    for row in rows:
        if row["data_id"] and row["data_id"] not in seen:
            seen.add(row["data_id"])
            restaurants.append(row)
    return restaurants


SOURCE_SEARCH = merge_reviews.SOURCE_SERPAPI_SEARCH   # returned by a filtered (query / topic) search
SOURCE_UNFILTERED = "unfiltered"    # part of an unfiltered download (SerpApi or Apify)
is_filtered_output = merge_reviews.is_filtered_output


def _items(merged: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Merged reviews as ``{"review", "sources"}`` items, unfiltered sources also tagged SOURCE_UNFILTERED."""
    return {data_id: [{"review": r, "sources": set(r["sources"]) | (
                {SOURCE_UNFILTERED} if merge_reviews.UNFILTERED_SOURCES & set(r["sources"]) else set())}
                      for r in reviews]
            for data_id, reviews in merged.items()}


def collected_reviews(output_dirs: Optional[list[Path]] = None) -> dict[str, list[dict]]:
    """
    Every collected review per data_id, merged across sources by
    merge_reviews (deduplicated by review_id).

    Each item is ``{"review": <review>, "sources": {...}}``: the sources that
    found it, plus SOURCE_UNFILTERED for a SerpApi or Apify unfiltered
    download. Washroom searches only return washroom reviews, so only the
    unfiltered reviews show how common washroom talk is.
    """
    dirs = None
    if output_dirs:
        known = {folder: source for folder, source in merge_reviews.source_dirs()}
        dirs = [(folder, known.get(folder, merge_reviews.SOURCE_SERPAPI)) for folder in output_dirs]
    return _items(merge_reviews.load_sources(dirs))


# ---------------------------------------------------------------------------
# Building the CSV rows
# ---------------------------------------------------------------------------

def build_rows(restaurants: list[dict], reviews_by_id: dict[str, list[dict]],
               sample_size: int = DEFAULT_SAMPLE_SIZE) -> tuple[list[dict], list[dict]]:
    """
    ``(mention_rows, summary_rows)`` for *restaurants*, newest first per restaurant.

    One mention row per washroom-related review (any source), so
    ``washroom_related_reviews`` equals the restaurant's number of mention rows.
    ``washroom_share`` is measured on the restaurant's newest *sample_size*
    unfiltered reviews only: washroom reviews among those with text.
    *reviews_by_id* is what ``collected_reviews`` returns.
    """
    mentions: list[dict] = []
    summary: list[dict] = []
    for r in restaurants:
        items = reviews_by_id.get(r["data_id"], [])
        rows: list[dict] = []
        sample = sample_with_text = washroom_in_sample = from_search = 0
        for item in sorted(items, key=lambda x: x["review"].get("iso_date") or "", reverse=True):
            review = item["review"]
            snippets = extract_washroom_snippets(review.get("text"))
            in_sample = SOURCE_UNFILTERED in item["sources"] and sample < sample_size
            sample += in_sample
            sample_with_text += in_sample and bool((review.get("text") or "").strip())
            from_search += SOURCE_SEARCH in item["sources"]
            if not snippets:
                continue
            washroom_in_sample += in_sample
            rows.append({
                "mention_id":       review.get("review_id") or short_hash(make_review_fingerprint(review)),
                "restaurant":       r["restaurant"],
                "data_id":          r["data_id"],
                "review_id":        review.get("review_id") or "",
                "review_date":      review.get("date") or "",
                "iso_date":         review.get("iso_date") or "",
                "star_rating":      review.get("rating") if review.get("rating") is not None else "",
                "washroom_snippet": PASSAGE_JOINER.join(snippets),
                "passage_count":    len(snippets),
                "full_review_text": _plain(review.get("text") or ""),
                "review_link":      review.get("review_link") or "",
            })
        mentions.extend(rows)
        summary.append({
            "restaurant":               r["restaurant"],
            "data_id":                  r["data_id"],
            "google_review_count":      r.get("google_review_count", ""),
            "sample_reviews":           sample,
            "sample_reviews_with_text": sample_with_text,
            "washroom_in_sample":       washroom_in_sample,
            "washroom_share":           round(washroom_in_sample / sample_with_text, 3) if sample_with_text else "",
            "washroom_search_reviews":  from_search,
            "washroom_related_reviews": len(rows),
            "latest_mention_date":      max((m["iso_date"] for m in rows), default=""),
        })
    return mentions, summary


def write_csv(rows: list[dict], columns: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract washroom mentions from collected reviews (0 API credits).")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help="Newest unfiltered reviews per restaurant the washroom share is measured on "
                             "(default %(default)s).")
    return parser.parse_args(argv)


def coverage_warning(restaurants: list[dict]) -> Optional[str]:
    """
    Why the summary is not final yet, from collect_latest_reviews' coverage
    file (None when every restaurant has its newest-reviews sample).
    """
    path = utils.OUTPUT_DIR / "latest_reviews" / "coverage.json"
    if not path.exists():
        return ("No unfiltered download yet (run scripts/collect_latest_reviews.py): "
                "washroom counts come from Google's partial in-review search only.")
    try:
        cov = load_json(path)
        status = {r["data_id"]: r["status"] for r in cov.get("restaurants") or []}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return f"{to_project_relative(path)} is unreadable; coverage unknown."
    pending = [r for r in restaurants if status.get(r["data_id"]) not in ("complete", "target_met")]
    if pending:
        return (f"{len(pending)} of {len(restaurants)} restaurants have not reached the newest-"
                f"{cov.get('target')} review target: the summary is not final.")
    return None


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    restaurants = load_restaurants(args.csv)
    # Rebuild the merged dataset first, so the washroom files never lag behind it
    mentions, summary = build_rows(restaurants, _items(merge_reviews.build(restaurants)), args.sample_size)
    mentions_path = utils.PROCESSED_DIR / "washroom_mentions.csv"
    summary_path = utils.PROCESSED_DIR / "washroom_summary.csv"
    write_csv(mentions, MENTION_COLUMNS, mentions_path)
    write_csv(summary, SUMMARY_COLUMNS, summary_path)

    print(f"Restaurants                : {len(restaurants)}")
    print(f"Sample (newest {args.sample_size} each) : {sum(s['sample_reviews'] for s in summary)} reviews, "
          f"{sum(s['sample_reviews_with_text'] for s in summary)} with text, "
          f"{sum(s['washroom_in_sample'] for s in summary)} washroom-related")
    print(f"Washroom search results    : {sum(s['washroom_search_reviews'] for s in summary)}")
    print(f"Washroom-related reviews   : {sum(s['washroom_related_reviews'] for s in summary)}")
    print(f"Restaurants with none found: {sum(1 for s in summary if not s['washroom_related_reviews'])}")
    print(f"Mentions CSV               : {to_project_relative(mentions_path)}")
    print(f"Summary CSV                : {to_project_relative(summary_path)}")
    warning = coverage_warning(restaurants)
    if warning:
        print(f"\n[WARN] {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
