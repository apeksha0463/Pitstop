# Pitstop — Google Maps Review Extractor

A robust, production-ready Python tool that extracts restaurant reviews from Google Maps using the [SerpApi](https://serpapi.com/) `google_maps_reviews` engine.

It fetches reviews across multiple pages with automatic token-based pagination, deduplicates reviews, saves intermediate page snapshots, and persists clean, structured JSON datasets.

---

## 📁 Repository Structure

The repository is organized strictly into three top-level directories:

```text
Pitstop/
├── scripts/
│   ├── extract_reviews.py     # Main CLI entry point
│   ├── extract_pages.py       # Page persistence & resumption helper
│   ├── extract_images.py      # Review photo download utility
│   ├── utils.py               # Shared API, path, and normalization logic
│   ├── requirements.txt       # Python dependencies
│   ├── .env.example           # Example environment template
│   ├── .gitignore             # Ignore rules for scripts and cache
│   └── tests/                 # Full unit and integration test suite
│       ├── test_extract_reviews.py
│       ├── test_extract_pages.py
│       ├── test_extract_images.py
│       └── test_paths.py
│
├── data/
│   ├── raw_data/              # Raw SerpApi responses
│   ├── extracted_pages/       # Individual API response pages
│   ├── extracted_images/      # Downloaded review images
│   ├── processed/             # Cleaned and deduplicated reviews
│   ├── output/                # Final timestamped JSON datasets
│   └── .gitignore             # Excludes generated data from git
│
└── sources/
    ├── README.md              # Project documentation (this file)
    ├── project_documentation/ # Project guides and notes
    ├── api_documentation/     # SerpApi reference notes
    ├── .env                   # Private API key (never committed)
    └── .gitignore             # Protects credentials from git
```

---

## ⚙️ Prerequisites & Installation

### 1. Python Environment

Ensure Python 3.10+ (tested through Python 3.14) is installed.

```bash
python --version
```

### 2. Install Dependencies

Install required libraries using the requirements file in `scripts/`:

```bash
pip install -r scripts/requirements.txt
```

---

## 🔑 SerpApi Key Configuration

The extractor requires a valid SerpApi API key. The key must be placed in `sources/.env`.

1. Copy the example file or create `sources/.env`:

   ```bash
   cp scripts/.env.example sources/.env
   ```

2. Open `sources/.env` and replace the placeholder with your actual key:

   ```ini
   SERPAPI_API_KEY=your_actual_serpapi_key_here
   ```

> **Security Note:** `sources/.gitignore` automatically prevents `sources/.env` from being committed or pushed to Git.

---

## 🚀 How to Run the Review Extractor

Run the extractor directly from the repository root:

### Interactive Mode

You will be prompted to enter the Google Maps `data_id`:

```bash
python scripts/extract_reviews.py
```

### Non-Interactive CLI Mode

Provide the `data_id` and optional configuration flags directly:

```bash
python scripts/extract_reviews.py --data-id 0x3bb1e3f2fe781a35:0xa978b861803d2fbb
```

Or using module execution syntax:

```bash
python -m scripts.extract_reviews --data-id 0x3bb1e3f2fe781a35:0xa978b861803d2fbb
```

### Available CLI Options

| Option | Default | Description |
|---|---|---|
| `--data-id <ID>` | Prompted | Google Maps restaurant `data_id` |
| `--max-requests <N>` | `10` | Maximum API requests to perform (guards free credits) |
| `--no-raw-pages` | `False` | Skip saving individual raw responses to `data/extracted_pages/` |

---

## 💾 Output Location & Format

Extracted review files are saved automatically under:

```text
data/output/
```

Filename pattern:
```text
data/output/<restaurant_name>_<data_id>_<YYYYMMDD_HHMMSS>.json
```

### Output JSON Schema

```json
{
  "source": "SerpApi Google Maps Reviews",
  "data_id": "0x3bb1e3f2fe781a35:0xa978b861803d2fbb",
  "restaurant_name": "Swathi Delicacy",
  "address": "Opposite to Swathi Delicacy, NH 75, Koppa, Karnataka 571425",
  "rating": 4.1,
  "type": "South Indian restaurant",
  "phone": "+91 96111 26233",
  "website": "http://www.swathidelicacy.com/",
  "total_reviews_reported": 14210,
  "reviews_collected": 100,
  "duplicates_skipped": 0,
  "pages_fetched": 10,
  "collection_complete": false,
  "collection_status": "stopped_at_request_limit",
  "extracted_at": "2026-10-28T14:40:00.123456",
  "reviews": [
    {
      "review_id": "ChZDSUhNMG9nS0VJQ0FnSUNubV9tX2V3EAE",
      "author": "Karthik R",
      "author_url": "https://www.google.com/maps/contrib/...",
      "author_local_guide": true,
      "author_reviews_count": 52,
      "rating": 5,
      "date": "a week ago",
      "iso_date": "2026-10-21T00:00:00Z",
      "iso_date_of_last_edit": null,
      "text": "Excellent South Indian breakfast on the highway. Clean restrooms and fast service.",
      "language": "en",
      "translated": false,
      "likes": 4,
      "owner_response": null,
      "photos_count": 2,
      "photos": [
        "https://lh5.googleusercontent.com/p/..."
      ],
      "review_link": "https://www.google.com/maps/reviews/...",
      "source": "Google Maps via SerpApi"
    }
  ]
}
```

---

## 🧪 Running Tests

Execute the comprehensive test suite with `pytest`:

```bash
python -m pytest scripts/tests/ -v
```

All 64 tests run offline using mocks without consuming any SerpApi credits.

---

## ⚠️ SerpApi Free-Plan Limitations

- **Monthly Quota:** SerpApi's free tier provides 100 searches per month across all engines.
- **Reviews per Request:** Each API request returns approximately 10 reviews.
- **Request Cap:** By default, `MAX_REQUESTS` is set to 10 (fetching ~100 reviews per run) to protect your quota. You can adjust this with `--max-requests <N>`.
- **Pagination Expiry:** SerpApi `next_page_token` values expire quickly; requests are chained with respectful delay intervals (1.5s default).
