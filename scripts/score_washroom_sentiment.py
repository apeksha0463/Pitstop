"""
scripts/score_washroom_sentiment.py
===================================
Score how positive or negative each washroom mention is, locally (no API, no
credits), with the aspect-based sentiment model
``yangheng/deberta-v3-base-absa-v1.1`` (Hugging Face, runs on the CPU).

The model rates the sentiment towards one aspect of a text, so "Food was
amazing, but the washroom was filthy" scores negative for the washroom. Each
``washroom_snippet`` is scored with the washroom word the reviewer used
("restroom", "toilet", ...) as the aspect. A snippet made of several passages
is scored passage by passage and the probabilities are averaged.

sentiment_score = P(positive) - P(negative), from -1 to +1.

``needs_review`` marks the scores worth a human look: the model is unsure
(confidence below 0.6), or the passages of one review get different labels
(e.g. "toilets not cleaned ... other branches assure clean toilets"), where
the average can land on the wrong side. Checked on this data, most model
errors fall in these two groups.

The model (~740 MB) is downloaded once into the Hugging Face cache in the user
folder, never into the repository.

Reads:
  - data/processed/washroom_mentions.csv
Writes:
  - data/processed/washroom_mentions_scored.csv     mentions + sentiment columns
  - data/processed/washroom_sentiment_summary.csv   one row per restaurant

Usage (from the repository root):

    python scripts/score_washroom_sentiment.py
"""

from __future__ import annotations

import argparse
import csv
import datetime
import statistics
import sys
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                              # repository root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from scripts import utils
    from scripts.extract_washroom_mentions import (
        DEFAULT_CSV, MENTION_COLUMNS, PASSAGE_JOINER, find_washroom_terms, load_restaurants,
    )
    from scripts.utils import to_project_relative
except ImportError:
    import utils
    from extract_washroom_mentions import DEFAULT_CSV, MENTION_COLUMNS, PASSAGE_JOINER, find_washroom_terms, load_restaurants
    from utils import to_project_relative

MODEL_NAME = "yangheng/deberta-v3-base-absa-v1.1"
LABELS = ("negative", "neutral", "positive")
DEFAULT_ASPECT = "washroom"
BATCH_SIZE = 16
LOW_DATA_THRESHOLD = 5        # fewer scored reviews than this: flagged low_data
RECENT_YEARS = 2
MIN_CONFIDENCE = 0.6

SCORED_COLUMNS = MENTION_COLUMNS + ["aspect", "sentiment", "sentiment_score", "confidence",
                                    "p_negative", "p_neutral", "p_positive", "needs_review"]
SUMMARY_COLUMNS = [
    "restaurant", "data_id", "washroom_reviews", "positive", "neutral", "negative",
    "mean_sentiment_score", "share_negative", "recent_reviews", "recent_mean_sentiment_score",
    "needs_review", "latest_mention_date", "low_data",
]

# (texts, aspects) -> one [p_negative, p_neutral, p_positive] per text
Scorer = Callable[[list[str], list[str]], list[list[float]]]


def load_model_scorer(model_name: str = MODEL_NAME, batch_size: int = BATCH_SIZE) -> Scorer:
    """A scorer backed by the Hugging Face model (downloaded on first use)."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(model_name).eval()
    order = [next(i for i, name in model.config.id2label.items() if name.lower() == label) for label in LABELS]

    def score(texts: list[str], aspects: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        with torch.no_grad():
            for start in range(0, len(texts), batch_size):
                enc = tokenizer(texts[start:start + batch_size], aspects[start:start + batch_size],
                                return_tensors="pt", padding=True, truncation=True, max_length=256)
                probs = model(**enc).logits.softmax(-1)
                out.extend([[float(row[i]) for i in order] for row in probs])
        return out

    return score


def aspect_of(passage: str) -> str:
    """The washroom word used in *passage* (lower case), or "washroom"."""
    terms = find_washroom_terms(passage)
    return terms[0].lower() if terms else DEFAULT_ASPECT


def score_mentions(rows: list[dict], scorer: Scorer) -> list[dict]:
    """*rows* (washroom_mentions.csv) with the sentiment columns added."""
    passages: list[tuple[int, str, str]] = []
    for i, row in enumerate(rows):
        for passage in (row["washroom_snippet"] or "").split(PASSAGE_JOINER):
            if passage.strip():
                passages.append((i, passage.strip(), aspect_of(passage)))
    probs = scorer([p for _, p, _ in passages], [a for _, _, a in passages]) if passages else []
    per_row: dict[int, list[list[float]]] = {}
    aspects: dict[int, list[str]] = {}
    for (i, _, aspect), p in zip(passages, probs):
        per_row.setdefault(i, []).append(p)
        aspects.setdefault(i, []).append(aspect)
    scored = []
    for i, row in enumerate(rows):
        ps = per_row.get(i)
        if not ps:
            scored.append({**row, **{c: "" for c in SCORED_COLUMNS if c not in row}})
            continue
        avg = [statistics.fmean(col) for col in zip(*ps)]
        best = max(range(3), key=lambda k: avg[k])
        passage_labels = {max(range(3), key=lambda k: p[k]) for p in ps}
        scored.append({
            **row,
            "aspect":          ";".join(dict.fromkeys(aspects[i])),
            "sentiment":       LABELS[best],
            "sentiment_score": round(avg[2] - avg[0], 3),
            "confidence":      round(avg[best], 3),
            "p_negative":      round(avg[0], 3),
            "p_neutral":       round(avg[1], 3),
            "p_positive":      round(avg[2], 3),
            "needs_review":    avg[best] < MIN_CONFIDENCE or len(passage_labels) > 1,
        })
    return scored


def summarise(restaurants: list[dict], scored: list[dict], today: Optional[datetime.date] = None) -> list[dict]:
    """One row per restaurant (all of them, in list order)."""
    today = today or datetime.date.today()
    cutoff = (today.replace(year=today.year - RECENT_YEARS)).isoformat()
    summary = []
    for r in restaurants:
        rows = [x for x in scored if x["data_id"] == r["data_id"] and x.get("sentiment")]
        scores = [float(x["sentiment_score"]) for x in rows]
        recent = [float(x["sentiment_score"]) for x in rows if (x.get("iso_date") or "") >= cutoff]
        counts = {label: sum(x["sentiment"] == label for x in rows) for label in LABELS}
        summary.append({
            "restaurant":                  r["restaurant"],
            "data_id":                     r["data_id"],
            "washroom_reviews":            len(rows),
            "positive":                    counts["positive"],
            "neutral":                     counts["neutral"],
            "negative":                    counts["negative"],
            "mean_sentiment_score":        round(statistics.fmean(scores), 3) if scores else "",
            "share_negative":              round(counts["negative"] / len(rows), 3) if rows else "",
            "recent_reviews":              len(recent),
            "recent_mean_sentiment_score": round(statistics.fmean(recent), 3) if recent else "",
            "needs_review":                sum(x.get("needs_review") in (True, "True") for x in rows),
            "latest_mention_date":         max((x.get("iso_date") or "" for x in rows), default=""),
            "low_data":                    len(rows) < LOW_DATA_THRESHOLD,
        })
    return summary


def _write(rows: list[dict], columns: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score washroom mentions with a local sentiment model.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help="Restaurant list with a data_id column (default: %(default)s).")
    parser.add_argument("--model", default=MODEL_NAME, help="Hugging Face model (default: %(default)s).")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None, scorer: Optional[Scorer] = None) -> int:
    args = parse_args(argv)
    mentions_path = utils.PROCESSED_DIR / "washroom_mentions.csv"
    if not mentions_path.exists():
        print(f"[ERROR] {to_project_relative(mentions_path)} not found: run extract_washroom_mentions.py first.")
        return 1
    with open(mentions_path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    print(f"Scoring {len(rows)} washroom mention(s) with {args.model} (local, CPU)...")
    scored = score_mentions(rows, scorer or load_model_scorer(args.model))
    summary = summarise(load_restaurants(args.csv), scored)

    scored_path = utils.PROCESSED_DIR / "washroom_mentions_scored.csv"
    summary_path = utils.PROCESSED_DIR / "washroom_sentiment_summary.csv"
    _write(scored, SCORED_COLUMNS, scored_path)
    _write(summary, SUMMARY_COLUMNS, summary_path)

    counts = {label: sum(x.get("sentiment") == label for x in scored) for label in LABELS}
    print(f"Positive / neutral / negative : {counts['positive']} / {counts['neutral']} / {counts['negative']}")
    print(f"Flagged needs_review          : {sum(x.get('needs_review') is True for x in scored)}")
    print(f"Restaurants with enough data  : {sum(not s['low_data'] for s in summary)} / {len(summary)}"
          f" (at least {LOW_DATA_THRESHOLD} washroom reviews)")
    print(f"Scored mentions               : {to_project_relative(scored_path)}")
    print(f"Per-restaurant summary        : {to_project_relative(summary_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
