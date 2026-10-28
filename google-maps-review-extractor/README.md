# Google Maps Review Extractor

Extract restaurant reviews from Google Maps using the [SerpApi](https://serpapi.com/) `google_maps_reviews` engine. Fetches all available review pages with automatic pagination, deduplicates results, and saves clean JSON to the `output/` directory.

---

## Features

- 🔍 Fetches reviews by Google Maps `data_id`
- 📄 Automatic pagination through all available review pages
- 🔁 Deduplication across pages (by `review_id` or content fingerprint)
- 💾 Intermediate raw page data saved to `data/extracted_pages/` for debugging
- 🖼️ Optional review image downloader (`scripts/extract_images.py`)
- 🔒 API key loaded from `.env` — never hardcoded or written to output files
- 🛡️ Configurable request cap (protects free-plan SerpApi credits)
- ⚙️ Bounded retries, timeouts, and rate-limit handling
- 🧪 Full test suite (no real API calls required)
- 🪟 Works on Windows and Linux via `pathlib`-based paths

---

## Folder Structure

```
google-maps-review-extractor/
│
├── scripts/                    # Python source modules
│   ├── __init__.py
│   ├── extract_reviews.py      # Main entry point
│   ├── extract_pages.py        # Raw page persistence helpers
│   ├── extract_images.py       # Review image downloader
│   └── utils.py                # Shared helpers (API, paths, JSON, dedup)
│
├── data/                       # Intermediate data (gitignored by default)
│   ├── raw_data/               # Reserved for future raw storage
│   ├── extracted_pages/        # Raw API page responses (one file per page)
│   ├── extracted_images/       # Downloaded review images
│   └── processed/              # Cleaned / normalised datasets
│
├── sources/                    # Source notes and data-origin documentation
│   └── README.md
│
├── output/                     # Final review JSON files (gitignored by default)
│   └── <restaurant>_<id>_<timestamp>.json
│
├── tests/                      # Automated test suite
│   ├── test_extract_reviews.py
│   ├── test_extract_pages.py
│   ├── test_extract_images.py
│   └── test_paths.py
│
├── .env                        # Your SerpApi key (NOT committed to git)
├── .env.example                # Key template — safe to commit
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Requirements

- Python 3.8+
- A [SerpApi](https://serpapi.com/) account (free plan: ~100 searches/month)

Install dependencies:

```bash
pip install -r requirements.txt
```

Dependencies:
- `requests>=2.31.0`
- `python-dotenv>=1.0.0`

---

## SerpApi Setup

1. Create a free account at [https://serpapi.com/](https://serpapi.com/).
2. Copy your API key from the dashboard.
3. Create a `.env` file in this project root:

```bash
cp .env.example .env
```

4. Edit `.env` and replace the placeholder:

```env
SERPAPI_API_KEY=your_actual_key_here
```

> ⚠️ **Never commit `.env` to Git.** It is already listed in `.gitignore`.

---

## How to Run

**From the project root** (`google-maps-review-extractor/`):

```bash
python -m scripts.extract_reviews
```

Or run the script directly:

```bash
python scripts/extract_reviews.py
```

### Command-line options

```
python -m scripts.extract_reviews --help

optional arguments:
  --data-id DATA_ID       Google Maps restaurant data_id (prompted if omitted)
  --max-requests N        Maximum SerpApi requests (default: 10 ≈ 100 reviews)
  --no-raw-pages          Skip saving raw API responses to data/extracted_pages/
```

### Non-interactive example

```bash
python -m scripts.extract_reviews --data-id "0x3bb1e3f2fe781a35:0xa978b861803d2fbb" --max-requests 5
```

---

## Finding a Restaurant's data_id

1. Go to [Google Maps](https://maps.google.com) and search for the restaurant.
2. Click the restaurant. The URL will contain a `1s` segment like:
   ```
   https://www.google.com/maps/place/.../@.../data=...!1s0x3bb...:0xa97...
   ```
3. The hex string (`0x....:0x....`) is the `data_id`.
4. Alternatively, use the SerpApi [Google Maps Search API](https://serpapi.com/google-maps-api) to search by name and extract the `data_id` from results.

---

## Output Files

### Final review JSON — `output/`

```
output/swathi_delicacy_0x3bb1e..._20261028_120000.json
```

Structure:

```json
{
  "source": "SerpApi Google Maps Reviews",
  "data_id": "0x3bb1...:0xa97...",
  "restaurant_name": "Swathi Delicacy",
  "address": "...",
  "rating": 4.3,
  "total_reviews_reported": 1200,
  "reviews_collected": 97,
  "duplicates_skipped": 3,
  "pages_fetched": 10,
  "collection_complete": false,
  "collection_status": "stopped_at_request_limit",
  "extracted_at": "2026-10-28T12:00:00.000000",
  "reviews": [...]
}
```

> `collection_complete: true` only when all pages were exhausted naturally.  
> `collection_status` values: `completed`, `stopped_at_request_limit`, `stopped_on_error`, `interrupted_by_user`.

### Raw page responses — `data/extracted_pages/`

Each API page response is saved as:
```
data/extracted_pages/<safe_data_id>_page001.json
```

Disable with `--no-raw-pages`.

### Downloaded images — `data/extracted_images/`

Use `scripts/extract_images.py` to download review photos:

```python
from scripts.extract_images import download_review_images
import json

with open("output/my_restaurant.json") as f:
    data = json.load(f)

report = download_review_images(data["reviews"], data["data_id"])
print(report)
```

---

## Free-Plan Limits

| Plan  | Searches/month | Reviews per search |
|-------|---------------:|-------------------:|
| Free  | ~100           | ~10                |

- Each paginated page counts as **one search**.
- Default `--max-requests 10` uses 10 credits per restaurant run.
- Adjust with `--max-requests` to use fewer or more credits.
- The script stops safely and saves partial results when quota is exhausted.

---

## Error Handling

| Situation | Behaviour |
|-----------|-----------|
| Missing / placeholder API key | Prints error and exits immediately |
| HTTP 401 Unauthorized | Raises immediately — check your `.env` |
| HTTP 429 Rate Limited | Waits 30 s × attempt, then retries |
| HTTP 5xx Server Error | Waits 10 s × attempt, then retries |
| Quota exhausted (JSON error) | Saves partial results and exits |
| Network timeout | Retries up to `MAX_RETRIES` (default: 3) |
| `Ctrl+C` / KeyboardInterrupt | Saves collected reviews and exits |

---

## Running Tests

Tests run entirely with mocked API calls — no SerpApi credits are consumed.

```bash
# Run all tests from the project root
python -m pytest tests/ -v

# Or run individual test files
python tests/test_extract_reviews.py
python tests/test_extract_pages.py
python tests/test_extract_images.py
python tests/test_paths.py
```

---

## Contributing & Pushing to GitHub

1. Install dependencies and run all tests:
   ```bash
   pip install -r requirements.txt
   python -m pytest tests/ -v
   ```

2. Check that no secrets are staged:
   ```bash
   git diff --staged | grep -i "api_key"
   ```

3. Stage and commit your changes:
   ```bash
   git add .
   git status        # verify .env is NOT listed
   git commit -m "your: descriptive commit message"
   ```

4. Push to GitHub:
   ```bash
   git push origin master
   ```

> ⚠️ Never force-push (`git push --force`) unless you know exactly what you're doing.

---

## Project Layout Quick Reference

| Path | Purpose |
|------|---------|
| `scripts/extract_reviews.py` | **Run this** to extract reviews |
| `scripts/utils.py` | Shared API, path, and data helpers |
| `scripts/extract_pages.py` | Raw page save/load/list utilities |
| `scripts/extract_images.py` | Review image downloader |
| `output/` | Final JSON review datasets |
| `data/extracted_pages/` | Raw per-page API responses |
| `data/extracted_images/` | Downloaded review photos |
| `tests/` | Full automated test suite |
| `.env` | Your private API key (never commit!) |
| `.env.example` | Safe template to share/commit |
