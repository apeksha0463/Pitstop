# Google Maps Review Extractor

Automatically extract all publicly available reviews for any Google Maps restaurant using the **SerpApi Google Maps Reviews API**. No manual copying. No browser scraping. Pure API automation with full pagination support.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Installation](#installation)
3. [API Key Setup](#api-key-setup)
4. [Running the Script](#running-the-script)
5. [How to Find the Restaurant data\_id](#how-to-find-the-restaurant-data_id)
6. [Pagination](#pagination)
7. [Output Format](#output-format)
8. [Free Plan Limitations](#free-plan-limitations)
9. [Configuration](#configuration)
10. [Testing](#testing)
11. [Common Errors and Fixes](#common-errors-and-fixes)
12. [Project Structure](#project-structure)

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add your SerpApi key to .env
echo SERPAPI_API_KEY=your_real_key_here > .env

# 3. Run
python get_reviews.py

# 4. Enter your restaurant data_id when prompted
```

---

## Installation

### Prerequisites

- Python 3.8 or higher
- A [SerpApi account](https://serpapi.com/) (free plan available)
- Internet connection

### Steps

```bash
# Clone or copy this project folder, then:
cd google-maps-review-extractor
pip install -r requirements.txt
```

**Dependencies installed:**
| Package | Purpose |
|---|---|
| `requests` | HTTP calls to SerpApi |
| `python-dotenv` | Loads `.env` securely |

---

## API Key Setup

1. Sign up at [serpapi.com](https://serpapi.com/) and copy your API key from the dashboard.
2. Open (or create) the `.env` file in this folder:

```env
SERPAPI_API_KEY=paste_your_real_key_here
```

3. **Never commit `.env` to Git.** It is already listed in `.gitignore`.
4. Use `.env.example` as a safe template to share with teammates.

The script will validate your key before making any requests and will exit with a clear error if it is missing or still a placeholder.

---

## Running the Script

```bash
python get_reviews.py
```

You will be prompted:

```
Enter the Google Maps restaurant data_id.
(Example format: 0x3bae160000000000:0x123456789abcdef0)

Restaurant data_id: 0x549069a98254bd17:0xb2f64f75b3edf4c3
```

The script then:
1. Validates your API key.
2. Fetches the first page of reviews.
3. Displays restaurant name, address, and rating.
4. Automatically paginates through all available pages.
5. Saves results to `output/restaurant_reviews_<id>_<timestamp>.json`.

---

## How to Find the Restaurant data_id

The `data_id` uniquely identifies a place on Google Maps. To find it:

### Method 1 — SerpApi Google Maps Search
1. Use the [SerpApi Google Maps endpoint](https://serpapi.com/google-maps-api) to search by name/query.
2. In the response, each result has a `"data_id"` field — copy that.

### Method 2 — Google Maps URL
Open Google Maps in a browser, search for the restaurant, and look at the URL:

```
https://www.google.com/maps/place/.../data=!4m...!1s0x549069a98254bd17:0xb2f64f75b3edf4c3
```

The hex value after `!1s` is the `data_id`.

### Method 3 — SerpApi Playground
Use [serpapi.com/playground](https://serpapi.com/playground) with `engine=google_maps` and your query to get `data_id` values from the results.

---

## Pagination

SerpApi uses a **token-based pagination** system for Google Maps Reviews:

```
Page 1 request  →  response includes serpapi_pagination.next_page_token
Page 2 request  →  params["next_page_token"] = token_from_page_1
Page 3 request  →  params["next_page_token"] = token_from_page_2
...
Last page       →  no next_page_token in response → stop
```

The script:
- Automatically extracts `serpapi_pagination.next_page_token` from every response.
- Passes it to the next request.
- Stops when the token is absent (no more pages) or the `MAX_REQUESTS` limit is reached.
- **Never re-fetches the same page** — each token is unique.
- Deduplicates reviews across pages using `review_id` (with fallback fingerprinting).

> **Note:** The deprecated `start` offset parameter is not used. Token-based pagination is the only method supported by SerpApi for this engine.

---

## Output Format

Results are saved in `output/` as JSON:

```json
{
  "source": "SerpApi Google Maps Reviews",
  "data_id": "0x549069a98254bd17:0xb2f64f75b3edf4c3",
  "restaurant_name": "Space Needle",
  "address": "400 Broad St, Seattle, WA 98109",
  "rating": 4.6,
  "type": "Tourist attraction",
  "phone": "+1 206-905-2100",
  "website": "https://www.spaceneedle.com/",
  "total_reviews_reported": 86420,
  "reviews_collected": 80,
  "duplicates_skipped": 0,
  "pages_fetched": 8,
  "collection_complete": false,
  "collection_status": "stopped_at_request_limit",
  "extracted_at": "2026-10-27T10:15:00.000000",
  "reviews": [
    {
      "review_id": "ChZDSUhNMG9nS0VJQ0FnSUMzOXBhc0FRAB",
      "author": "Alice Smith",
      "author_url": "https://www.google.com/maps/contrib/...",
      "author_local_guide": true,
      "author_reviews_count": 42,
      "rating": 5,
      "date": "3 months ago",
      "iso_date": "2026-07-01T00:00:00Z",
      "iso_date_of_last_edit": null,
      "text": "Absolutely stunning views! The rotating restaurant...",
      "language": "en",
      "translated": false,
      "likes": 23,
      "owner_response": null,
      "photos_count": 3,
      "photos": ["https://lh5.googleusercontent.com/..."],
      "review_link": "https://www.google.com/maps/reviews/...",
      "source": "Google Maps via SerpApi"
    }
  ]
}
```

### Status Values

| `collection_status` | Meaning |
|---|---|
| `completed` | All pages retrieved successfully |
| `stopped_at_request_limit` | Hit `MAX_REQUESTS` — increase it to get more |
| `stopped_on_error` | API/network error after some pages — partial data saved |
| `interrupted_by_user` | User pressed Ctrl+C — partial data saved |
| `in_progress` | Only appears in periodic progress saves |

---

## Free Plan Limitations

SerpApi's free plan includes **100 searches per month**.

| Fact | Detail |
|---|---|
| Searches per month | 100 (free plan) |
| Reviews per API call | ~10 (varies by place) |
| Max reviews with 10 calls | ~100 reviews |
| Reviews on popular restaurants | Thousands — full extraction requires a paid plan |

### Important Disclaimers

- **You cannot guarantee extracting all historical reviews** on a free plan.
- Google Maps does not always expose all reviews through any API.
- SerpApi may return fewer pages than the total review count suggests.
- The `total_reviews_reported` field is what Google reports; `reviews_collected` is what the API actually returned.

To extract more reviews:
1. Increase `MAX_REQUESTS` in `get_reviews.py` (uses more credits).
2. Upgrade to a paid SerpApi plan.

---

## Configuration

Edit the constants at the top of `get_reviews.py`:

```python
MAX_REQUESTS = 10         # Max API calls (each = ~10 reviews, = 1 credit)
REQUEST_DELAY_SECONDS = 1.5  # Pause between requests (rate limit protection)
REQUEST_TIMEOUT_SECONDS = 30 # HTTP timeout
MAX_RETRIES = 3           # Retries on transient network errors
SAVE_EVERY_N_PAGES = 5    # Save progress snapshot every N pages (0 = disable)
```

---

## Testing

Run the full test suite **without using any API credits**:

```bash
python test_get_reviews.py
```

Tests cover:
- Review fingerprinting and deduplication
- Review field flattening/normalization
- Place info extraction
- Multi-page pagination following
- Stopping at MAX_REQUESTS limit
- Empty review pages
- Quota exhaustion with partial save
- API key validation
- Secret leakage prevention
- Output JSON validity

---

## Common Errors and Fixes

### `[ERROR] SERPAPI_API_KEY not found.`
→ Create a `.env` file with `SERPAPI_API_KEY=your_key`

### `[ERROR] Replace the placeholder in .env with your real SerpApi key.`
→ Edit `.env` and replace `YOUR_PRIVATE_API_KEY_HERE`

### `[ERROR] Invalid API key (HTTP 401).`
→ Your key is wrong or expired. Check the SerpApi dashboard.

### `[ERROR] SerpApi credit/quota exhausted.`
→ Your monthly free credits (100) are used up. Wait for next month or upgrade.

### `[ERROR] Bad request (HTTP 400). The data_id may be invalid or malformed.`
→ Double-check the `data_id`. Make sure you copied it correctly (it contains a colon `:` and hex values).

### `[ERROR] Cannot connect to SerpApi.`
→ Check your internet connection. SerpApi may be temporarily down.

### `[WARN] Rate limited (HTTP 429).`
→ The script automatically retries with back-off. No action needed.

### No reviews returned despite a valid data_id
→ The restaurant may have no reviews, or SerpApi may not index it yet. Try verifying on Google Maps.

---

## Project Structure

```
google-maps-review-extractor/
├── get_reviews.py          # Main extraction script
├── test_get_reviews.py     # Full test suite (no API credits used)
├── .env                    # Your private API key (never commit!)
├── .env.example            # Safe template for teammates
├── .gitignore              # Excludes .env and other sensitive files
├── requirements.txt        # Python dependencies
├── README.md               # This file
└── output/                 # Auto-created; contains extracted JSON files
    └── restaurant_reviews_<id>_<timestamp>.json
```

---

## License

MIT — use freely, attribution appreciated.
