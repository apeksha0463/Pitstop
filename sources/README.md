# Pitstop — Google Maps Review Image Extractor

Pitstop collects Google Maps reviews for a restaurant through the
[SerpApi Google Maps Reviews API](https://serpapi.com/google-maps-reviews-api).
It extracts the **images that customers attached to their reviews**, downloads
them locally and saves them, linked to their reviews, in one JSON file per
restaurant.

> Review images only. Pitstop reads each review's own `images` field. It never
> substitutes general restaurant photos from the Google Maps Photos API. A
> review without images gets `"images": []`.

---

## Repository structure

```text
Pitstop/
├── scripts/
│   ├── extract_review_images.py   # MAIN entry point: reviews + review-image downloads
│   ├── extract_reviews.py         # Pagination/collection core (reviews + image URLs only)
│   ├── utils.py                   # Paths, API key, SerpApi requests, normalisation, JSON I/O
│   ├── requirements.txt
│   ├── .env.example               # Template for sources/.env
│   ├── .gitignore
│   └── tests/                     # Offline, fully mocked test suite
│       ├── test_extract_reviews.py
│       ├── test_extract_review_images.py
│       └── test_paths.py
├── data/
│   ├── raw_data/                  # Seed CSV + raw SerpApi responses (raw_data/serpapi/)
│   ├── extracted_pages/           # Page-level extracted reviews & image results
│   ├── extracted_images/          # Downloaded review images, one folder per restaurant
│   ├── processed/
│   ├── output/                    # Final JSON, one file per restaurant
│   └── .gitignore                 # Generated data and images are never committed
└── sources/
    ├── README.md                  # This file
    ├── .env                       # Your private SerpApi key (git-ignored, never committed)
    ├── .gitignore
    └── project_documentation/     # API reference notes
```

---

## 1. Installation

Requires Python 3.10+. In Windows PowerShell, from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r scripts/requirements.txt
```

## 2. API key configuration

1. Create `sources/.env` from the template:

   ```powershell
   Copy-Item scripts/.env.example sources/.env
   ```

2. Open `sources/.env` and replace the placeholder with your key from
   <https://serpapi.com/manage-api-key>:

   ```env
   SERPAPI_API_KEY=PASTE_YOUR_NEW_KEY_HERE
   ```

Security rules the code enforces:

- The key is read only from `sources/.env` via `python-dotenv`. A key there
  overrides any stale `SERPAPI_API_KEY` environment variable.
- The key is never printed, never written to JSON, and never included in error
  messages. Every saved API response has `api_key` fields and values redacted.
- `sources/.env` is git-ignored (`sources/.gitignore`).
- Before any search, the key is checked against SerpApi's Account API. That
  check does **not** use a search credit. An invalid key, or an account with
  0 searches left, stops the run with a clear message.

## 3. Running

```powershell
python scripts/extract_review_images.py
```

```text
PITSTOP REVIEW IMAGE EXTRACTOR

Enter Google Maps restaurant data_id: 0x3bae19cd83892495:0xc4ee26c182749dfe

Checking SerpApi key (account check, uses no search credits)...
  Key OK. Plan: Free Plan | searches left: ...

Fetching review page 1...
Reviews collected: ...
Reviews with images: ...
Review images found: ...
Images downloaded: ...
...
Final JSON: data/output/<Restaurant>_<data_id>.json
Images folder: data/extracted_images/<data_id>/
Collection status: completed
```

### Finding a restaurant's `data_id`

The `data_id` looks like `0x3bae19cd83892495:0xc4ee26c182749dfe`. You can find it:

- in the restaurant's Google Maps URL, after `!1s` (for example
  `...!1s0x3bae19cd83892495:0xc4ee26c182749dfe!...`), or
- as the `data_id` field in SerpApi Google Maps search results.

Paste it exactly when prompted. Surrounding quotes or spaces are removed automatically.

### Options

| Option | Default | Description |
|---|---|---|
| `--data-id ID` | prompted | Skip the prompt |
| `--max-requests N` | `5` | Max SerpApi searches this run (1 request = 1 credit) |
| `--max-images N` | no limit | Max image downloads this run |
| `--from-cache` | off | Rebuild from the last saved raw responses; **0 credits** |
| `--no-download` | off | Record image URLs but don't download files |
| `--skip-key-check` | off | Skip the free account check |

`python scripts/extract_reviews.py` runs the same collection without downloading
images (add `--download-images` to download them).

The scripts resolve every path relative to the repository root, so they work
from any working directory, including PowerShell and the VS Code terminal.

---

## 4. How review image extraction works

1. **Request.** `GET https://serpapi.com/search.json` with
   `engine=google_maps_reviews`, `data_id`, `hl=en` and `sort_by=newestFirst`.
2. **Paginate.** Each next page uses `serpapi_pagination.next_page_token`. On
   those requests Pitstop also sends `num=20` (SerpApi's maximum). The first
   page uses SerpApi's default page size, as a precaution: SerpApi may reject
   `num` on a request without a token, and a failed first request would waste
   the run. The run stops when there is no next token (**completed**),
   when the request budget is used up, or when the API reports an error.
3. **Extract images per review.** Each review's `images` field is parsed. The
   current API returns a list of URL strings. Object entries (`image`, `url`,
   `link`, `original`, `thumbnail`, ...), single values and the legacy `photos`
   field are also handled. Entries without an http(s) URL are dropped, so no
   image is ever invented. The reviewer's profile picture is not a review
   image and is never collected.
4. **Download promptly.** Image URLs are temporary, so images are downloaded
   right after each page:
   - sequential downloads, (5 s connect, 20 s read) timeouts, at most 2
     retries for timeouts, 429 or 5xx responses;
   - the response must be HTTP 200, have an `image/*` Content-Type (if one is
     sent) **and** start with real image bytes (JPEG/PNG/WebP/GIF/AVIF/HEIC/BMP).
     HTML error pages are rejected;
   - the extension comes from the file's bytes, never from the remote URL;
   - files are named `<review_id>_<n>.<ext>` and never overwritten. A
     different existing file gets a `_2`, `_3`, ... suffix;
   - identical images (same URL or same bytes) are stored once and referenced
     by every review that has them;
   - `_manifest.json` in each image folder lets later runs reuse files instead
     of downloading them again;
   - a failed download is recorded on that image and the run continues.
5. **Save after every page.** The output JSON is rewritten atomically after
   each page, so a crash, Ctrl+C or API error keeps everything collected so far.

### Output files

| Location | Content |
|---|---|
| `data/output/<Restaurant>_<data_id>.json` | Final dataset (one per restaurant; re-runs merge into it) |
| `data/extracted_images/<data_id>/` | Downloaded review images + `_manifest.json` |
| `data/raw_data/serpapi/<data_id>_pageNNN.json` | Full raw API responses (key removed) |
| `data/raw_data/serpapi/<data_id>_cache.json` | Which raw pages belong to the latest run (`--from-cache`) |
| `data/extracted_pages/<data_id>_pageNNN.json` | Per-page extracted reviews and image results |

All of these are git-ignored.

### JSON schema (abridged)

```json
{
  "source": "SerpApi Google Maps Reviews",
  "data_id": "0x...:0x...",
  "restaurant_name": "...",
  "address": "...",
  "total_reviews_reported": 3193,
  "reviews_collected": 60,
  "reviews_with_images": 12,
  "images_found": 31,
  "images_downloaded": 30,
  "images_failed": 1,
  "api_requests_used": 5,
  "collection_complete": false,
  "collection_status": "stopped_at_request_limit",
  "stop_reason": "Reached the request budget of 5. ...",
  "images_folder": "data/extracted_images/0x..._0x...",
  "reviews": [
    {
      "review_id": "...",
      "author": "...",
      "rating": 4,
      "date": "2 months ago",
      "text": "original review text",
      "images_count": 1,
      "images": [
        {
          "image_url": "https://lh3.googleusercontent.com/...",
          "image_index": 1,
          "local_path": "data/extracted_images/0x..._0x.../REVIEW_ID_1.jpg",
          "download_status": "downloaded",
          "content_type": "image/jpeg",
          "file_size_bytes": 33245,
          "sha256": "..."
        }
      ]
    }
  ]
}
```

`total_reviews_reported` is Google's count. `reviews_collected` is what this
dataset actually contains. `collection_complete` is `true` only when
pagination ended with no next-page token.

`download_status` values: `downloaded`, `already_downloaded` (reused an
existing identical file), `failed` (see `error` / `http_status`),
`not_an_image`, `invalid_url`, `skipped_limit`, `not_requested`.

`collection_status` values: `completed`, `stopped_at_request_limit`,
`stopped_credits_exhausted`, `stopped_invalid_api_key`, `stopped_on_error`,
`stopped_empty_page`, `interrupted_by_user`, `partial_from_cache`.

---

## 5. SerpApi free-plan limitations

- Every review page is **one search credit**. The free plan has a small
  monthly quota; the key check prints how many searches you have left.
- The default budget is **5 requests per run**. Raise it with
  `--max-requests N`. If your remaining credits are lower, the budget is
  reduced to match automatically.
- Page 1 returns about 10 reviews and later pages up to 20. A restaurant with
  thousands of reviews needs many credits; partial collection is normal and
  is reported honestly.
- The key check and image downloads do not use SerpApi credits. Image
  downloads go to Google's image servers, which have their own limits, so
  they run one at a time with a short delay.
- `--from-cache` rebuilds the dataset from saved responses with **no** API
  calls. SerpApi may also serve an identical repeated search from its own
  cache.
- Pitstop never bypasses authentication, rate limits or plan restrictions.
  It does not call the Google Maps Photos API.

---

## 6. Troubleshooting

| Message | Fix |
|---|---|
| `SERPAPI_API_KEY not found` | Create `sources/.env` (see step 2) |
| `Replace the placeholder in sources/.env` | Paste your real key over `PASTE_YOUR_NEW_KEY_HERE` |
| `SerpApi rejected the API key` | The key is wrong or revoked. Copy it again from the SerpApi dashboard |
| `no searches left` / `stopped_credits_exhausted` | Monthly quota used up. Wait for the reset; meanwhile use `--from-cache` |
| `That does not look like a Google Maps data_id` | It must look like `0x…:0x…` |
| `Bad request (HTTP 400)` | The `data_id` doesn't exist or is malformed |
| `Rate limited (HTTP 429)` | Wait and retry; Pitstop retries a few times automatically |
| Many images `failed` with HTTP 403/404 | Image URLs expired. Re-run to fetch fresh URLs, or download sooner |
| `not_an_image` | The server returned an HTML/error page instead of an image; nothing was saved |
| PowerShell blocks `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ModuleNotFoundError` | Activate the venv and run `pip install -r scripts/requirements.txt` |

---

## 7. Tests

All tests are offline and mocked. They make no SerpApi calls, use no credits,
and download nothing:

```powershell
python -m pytest scripts/tests -v
```

They cover reviews with zero, one or many images, different image field
formats, downloads and failures, HTML error pages, duplicate reviews and
images, overwrite protection, pagination and missing tokens, API errors,
exhausted credits and request budgets, partial saves after each page, the
zero-credit cache, file paths, the repository layout and secret protection.
