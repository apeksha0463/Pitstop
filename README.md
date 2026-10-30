# Pitstop — Google Maps Review Image Extractor

Pitstop collects Google Maps reviews for a restaurant through the
[SerpApi Google Maps Reviews API](https://serpapi.com/google-maps-reviews-api).
For each restaurant it downloads the **main restaurant photo** shown on the
Google Maps listing and saves everything in one JSON file per restaurant, with the main
photo listed first.

> Image categories are never mixed. Each image has an `image_type`:
> `main_restaurant_photo`, `restaurant_photo` or `customer_review_photo`.
> Review images come only from each review's own `images` field. A review
> without images gets `"images": []`. A review photo is never used as the main photo.

---

## Repository structure

```text
Pitstop/
├── scripts/
│   ├── extract_review_images.py   # MAIN entry point: reviews + review-image downloads
│   ├── extract_reviews.py         # Pagination/collection core (reviews + image URLs only)
│   ├── restaurant_photos.py       # Main restaurant photo + listing photos, image gallery
│   ├── utils.py                   # Paths, API key, SerpApi requests, normalisation, JSON I/O
│   ├── collect_latest_reviews.py  # Newest N unfiltered reviews of every listed restaurant
│   ├── collect_apify_reviews.py   # Same via Apify (when SerpApi pages come back truncated)
│   ├── merge_reviews.py           # All sources -> one dataset, each review once (0 credits)
│   ├── score_washroom_sentiment.py # Local aspect sentiment of every washroom mention
│   ├── collect_washroom_reviews.py # Washroom review searches for every listed restaurant
│   ├── extract_washroom_mentions.py # Washroom snippets -> data/processed/*.csv (0 credits)
│   ├── requirements.txt
│   ├── .env.example               # Template for sources/.env
│   ├── .gitignore
│   └── tests/                     # Offline, fully mocked test suite
│       ├── test_extract_reviews.py
│       ├── test_extract_review_images.py
│       ├── test_restaurant_photos.py
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
- `collect_apify_reviews.py` reads `APIFY_TOKEN=...` from the same file
  (Apify Console → Settings → API & Integrations). It is never printed either.
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

Fetching restaurant details (main photo)...
Main photo         : data/extracted_images/<data_id>/restaurant_main.jpg
Restaurant photos  : 3

Request budget left for review pages: 4

Fetching review page 1...
Reviews collected: ...
Reviews with images: ...
Review images found: ...
Images downloaded: ...
...
  IMAGE COLLECTION SUMMARY
  Main listing photo downloaded : Yes
  Reviews collected             : ... (of ... reported by Google)
  Reviews containing photos     : ...
  Review image URLs found       : ...
  Review images downloaded      : ...
  Failed downloads              : ...
  Pages fetched                 : ...
  API requests used             : ...
  JSON output                   : data/output/<data_id>_reviews.json
  Image folder                  : data/extracted_images/<data_id>/
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
| `--no-main-photo` | off | Skip the restaurant details lookup (saves 1 credit) |
| `--refresh-place` | off | Look up restaurant details again instead of using the cache |
| `--restaurant-photo-pages N` | `0` | Also fetch N pages from the Google Maps Photos API (1 credit each) |
| `--fresh` | off | Send SerpApi `no_cache=true` (bypass SerpApi's cached result, e.g. when a page looks truncated) |

`python scripts/extract_reviews.py` runs the same collection without downloading
images (add `--download-images` to download them).

The scripts resolve every path relative to the repository root, so they work
from any working directory, including PowerShell and the VS Code terminal.

---

## 4. How the main restaurant photo is found

The reviews response has no restaurant image, so the restaurant is looked up
**before** the reviews with SerpApi's Google Maps API (Place Results):

1. **Request.** `engine=google_maps`, `data_cid=<decimal of the second half of
   the data_id>`, `hl=en`. SerpApi documents `data_cid` as usable on its own, so
   the same `data_id` identifies the restaurant. No search by name is needed.
2. **Verify.** The result is used only if `place_results.data_id` (or
   `data_cid`) equals the requested `data_id`. Otherwise no photo is taken and
   `main_restaurant_photo_unavailable_reason` explains why.
3. **Select.** The main photo is `place_results.thumbnail`, the listing's
   primary image. If it is missing, a listing category image (`images[]`,
   preferring "All") is used and marked `"is_fallback": true`. If there is no
   image at all, `main_restaurant_photo` is `null` with a reason.
4. **Download first.** It is saved as `restaurant_main.<ext>`, using the same
   validation as review images (the extension comes from the image bytes, and
   an existing different file is never overwritten).
5. **Other listing photos.** `place_results.images` (one per category, such as
   Menu or Latest) are saved as `restaurant_photo_<n>.<ext>`. They cost no extra
   credit. Resized copies of the main photo are skipped. With
   `--restaurant-photo-pages N`, the Google Maps Photos API adds more.
6. **Cache.** The response is saved as
   `data/raw_data/serpapi/<data_id>_place.json` and reused on later runs (0
   credits). Use `--refresh-place` to look the restaurant up again.


### Output files

| Location | Content |
|---|---|
| `data/output/<data_id>_reviews.json` | Final dataset (one per restaurant; re-runs merge into it, and reviews from older `<Restaurant>_<data_id>.json` files are merged in) |
| `data/extracted_images/<data_id>/` | `restaurant_main.<ext>`, `restaurant_photo_<n>.<ext>`, review images `review_<review_id>_<n>.<ext>`, `_manifest.json` |
| `data/raw_data/serpapi/<data_id>_place.json` | Cached restaurant details (main photo source; key removed) |
| `data/raw_data/serpapi/<data_id>_pageNNN.json` | Full raw API responses (key removed) |
| `data/raw_data/serpapi/<data_id>_cache.json` | Which raw pages belong to the latest run (`--from-cache`) |
| `data/extracted_pages/<data_id>_pageNNN.json` | Per-page extracted reviews and image results |

All of these are git-ignored.

### JSON schema (abridged)

```json
{
  "restaurant_name": "...",
  "data_id": "0x...:0x...",
  "main_restaurant_photo": {
    "image_url": "https://lh5.googleusercontent.com/p/...=w...-h...-k-no",
    "image_type": "main_restaurant_photo",
    "source_field": "place_results.thumbnail",
    "selection_method": "primary_listing_thumbnail",
    "is_fallback": false,
    "local_path": "data/extracted_images/0x..._0x.../restaurant_main.jpg",
    "download_status": "downloaded"
  },
  "image_gallery": "... (see below)",
  "reviews": "... (see below)",
  "image_summary": {
    "main_photo_downloaded": true, "main_photo_is_fallback": false,
    "reviews_collected": 60, "reviews_with_images": 12,
    "review_images_found": 31, "unique_review_image_urls": 31,
    "review_images_downloaded": 30, "unique_review_image_files": 30,
    "review_image_download_failures": 1
  },
  "review_coverage": {
    "reviews_reported_by_google": 3193, "reviews_collected": 60,
    "all_reported_reviews_collected": false, "note": "..."
  },
  "address": "...",
  "total_reviews_reported": 3193,
  "main_restaurant_photo_unavailable_reason": null,
  "restaurant_photos": [
    {"image_url": "...", "image_type": "restaurant_photo", "category": "Menu",
     "local_path": "data/extracted_images/0x..._0x.../restaurant_photo_1.jpg", "download_status": "downloaded"}
  ],
  "place_lookup": {"engine": "google_maps", "data_cid": "...", "source": "api",
                   "identity_verified": true, "verified_by": "data_id", "returned_title": "..."},
  "reviews_collected": 60,
  "reviews_with_images": 12,
  "images_found": 31,
  "images_downloaded": 30,
  "images_failed": 1,
  "api_requests_used": 4,
  "restaurant_details_requests_used": 1,
  "api_requests_used_total": 5,
  "collection_complete": false,
  "collection_status": "stopped_at_request_limit",
  "stop_reason": "Reached the request budget of 5. ...",
  "images_folder": "data/extracted_images/0x..._0x...",
  "image_gallery": [
    {"position": 1, "image_type": "main_restaurant_photo", "local_path": ".../restaurant_main.jpg", "...": "..."},
    {"position": 2, "image_type": "customer_review_photo", "review_id": "...", "image_index": 1, "...": "..."},
    {"position": 3, "image_type": "restaurant_photo", "local_path": ".../restaurant_photo_1.jpg", "...": "..."}
  ],
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
          "image_type": "customer_review_photo",
          "local_path": "data/extracted_images/0x..._0x.../review_REVIEW_ID_1.jpg",
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

Customer-review photos come from two SerpApi sources, both attached to their
own review: the `images` of each review from the `google_maps_reviews` engine,
and the `images` of the "most relevant" reviews embedded in the cached place
details (`place_results.user_reviews.most_relevant`, 0 extra credits). The
newest reviews often have no photos, so the second source usually supplies
most of them. A review present in both is merged and its photos are not
downloaded twice.

`image_gallery` is always ordered: main listing photo, then customer review
photos, then other listing photos. Without a main photo it is `null` and the
review photos come first; a review photo is never promoted to main. The same
file is never listed twice, but `reviews[].images` keeps every review's own
links. The main photo is never counted in `image_summary`'s review counts.

`total_reviews_reported` is Google's count. `reviews_collected` is what this
dataset actually contains. `collection_complete` is `true` only when
pagination ended with no next-page token. That does not prove the whole
history was collected: `review_coverage.all_reported_reviews_collected` compares
the reviews collected with Google's count, and `note` explains any gap.

`download_status` values: `downloaded`, `already_downloaded` (reused an
existing identical file), `failed` (see `error` / `http_status`),
`not_an_image`, `invalid_url`, `skipped_limit`, `not_requested`.

`collection_status` values: `completed`, `stopped_at_request_limit`,
`stopped_credits_exhausted`, `stopped_invalid_api_key`, `stopped_on_error`,
`stopped_empty_page`, `interrupted_by_user`, `partial_from_cache`.

---

## 6. SerpApi free-plan limitations

- The restaurant details lookup (main photo) is **one credit**, used once per
  restaurant (later runs use the cache). It shares the `--max-requests`
  budget: with the default 5, that leaves 4 review pages. Skip it with
  `--no-main-photo`.
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
  It calls the Google Maps Photos API only when `--restaurant-photo-pages` is set.
- A quota or access error during the restaurant lookup is recorded in
  `place_lookup.api_error` and `main_restaurant_photo_unavailable_reason`.

---

## 7. Troubleshooting

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
| `main_restaurant_photo` is `null` | Read `main_restaurant_photo_unavailable_reason` (no credit left, lookup failed, place not verified, or no photo on the listing) |
| `not_an_image` | The server returned an HTML/error page instead of an image; nothing was saved |
| PowerShell blocks `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ModuleNotFoundError` | Activate the venv and run `pip install -r scripts/requirements.txt` |

---

## 8. Washroom reviews (for later sentiment scoring)

Three steps collect the washroom-related reviews of every restaurant in
`data/raw_data/restaurants_route_above_3.5.csv` (`data_id` column) and turn
them into plain-text snippets ready for an external sentiment API. No
sentiment or scoring is done here.

### Step 0 (recommended): newest reviews of every restaurant (`collect_latest_reviews.py`)

Google's in-review search misses many washroom reviews (see the coverage
limit under Step 2). This step downloads the newest N reviews of every
restaurant with no filter, so the washroom matching in Step 3 runs on a
known, complete sample.

```powershell
python scripts/collect_latest_reviews.py --report                 # coverage only, 0 credits
python scripts/collect_latest_reviews.py --max-credits 40         # newest 100 (default)
python scripts/collect_latest_reviews.py --max-reviews 200 --max-credits 40   # later: go deeper
```

- **Breadth first.** Page 1 for every restaurant, then page 2, and so on, so
  a small budget is spread evenly. Page 1 holds about 10 reviews and later
  pages 20. The newest 100 reviews of all 47 restaurants take about 270
  credits.
- **Resumable.** Each page is saved straight away, and where each restaurant
  stopped is kept in `data/output/latest_reviews/progress.json`. The next run
  continues from there with next month's credits, or with a larger
  `--max-reviews`. Pages saved by earlier runs are reused.
- **Coverage per restaurant** (`data/output/latest_reviews/coverage.json`):

  | Status | Meaning |
  |---|---|
  | `target_met` | At least N of the newest reviews downloaded |
  | `complete` | Every review downloaded; fewer than N exist |
  | `in_progress` | Some pages downloaded, more needed |
  | `not_started` | Nothing downloaded yet |
  | `ended_early` | SerpApi stopped offering pages well short of N and of Google's count |

- **Truncated results.** At times SerpApi returns only 5 reviews and no next
  page for every place. This was seen on 29 and 30 Oct 2026, and `no_cache`
  did not help. Such restaurants become `ended_early` and are only fetched
  again with `--retry-ended-early`. After 3 such pages in a row, the run stops
  to save credits.
- **Not final until covered.** `extract_washroom_mentions.py` warns until
  every restaurant is `target_met` or `complete`.

### Step 0, alternative: newest reviews through Apify (`collect_apify_reviews.py`)

When SerpApi keeps returning truncated pages, Apify's Google Maps Reviews
Scraper (`compass/Google-Maps-Reviews-Scraper`) downloads the same reviews.
Its free plan includes $5 of usage a month, and on that plan it charges
$0.0006 per review. 100 reviews cost $0.06, and the newest 100 of all 47
restaurants cost about $2.82.

```powershell
python scripts/collect_apify_reviews.py --limit 1 --max-charge-usd 0.1   # test one restaurant
python scripts/collect_apify_reviews.py --max-charge-usd 3               # newest 100 of the rest
python scripts/collect_apify_reviews.py --max-reviews 200 --max-charge-usd 3   # later: go deeper
```

- **Addressing.** Each restaurant is opened by its Google CID, the second
  part of its `data_id`, as `https://www.google.com/maps?cid=<cid>`. The
  returned place name is checked against the restaurant list.
- **Spending.** One actor run covers all restaurants. Its spend is capped with
  `maxTotalChargeUsd`. A cap that would exceed the month's limit is refused
  before anything runs.
- **Personal data.** Reviewer personal data is not requested
  (`personalData=false`).
- **Shared review IDs.** Review IDs are the same as SerpApi's, so a review
  found both ways counts once.
- **Skipping.** Restaurants that already have the target, or every review
  Google lists, are skipped unless `--refresh` is passed.
- **Outputs.** The raw dataset goes to `data/raw_data/apify/` and the reviews
  to `data/output/apify/<data_id>_reviews.json`. A saved dataset can be
  re-imported for free with `--from-dataset`.
- **Pipeline.** `extract_washroom_mentions.py` and the coverage report of
  `collect_latest_reviews.py` count these reviews as unfiltered.

### Step 1: filtered review searches (`extract_reviews.py --query / --topic-id`)

```powershell
python scripts/extract_reviews.py --data-id 0x3bae19cd83892495:0xc4ee26c182749dfe --query washroom
python scripts/extract_reviews.py --data-id 0x...:0x... --topic-id /m/0abc12
```

- `--query TEXT` and `--topic-id ID` are mutually exclusive (SerpApi rejects
  both together) and are sent as SerpApi `query` / `topic_id`.
- With either one set, page 1 also sends `num=20` (SerpApi only accepts `num`
  on the first page with a filter), so each credit returns up to 20 reviews.
- Every review gets `matched_by` (e.g. `["query:washroom", "query:toilet"]`).
  Reviews are still deduplicated by `review_id`, and the tags of a review
  found by several searches are merged.
- Output: `data/output/washroom/<data_id>_reviews.json`, one file per
  restaurant shared by all its searches, with `filter_runs` (one record per
  run, including the `next_page_token` to resume from) and the page-1
  `topics`. Raw pages go to `data/raw_data/serpapi/washroom/`. The unfiltered
  review history in `data/output/` is never merged in, and filtered runs skip
  the main-photo lookup.
- `utils.load_saved_topics(data_id)` reads the `topics` (`keyword`, `id`,
  `mentions`) from saved first pages for 0 credits. Filtered responses carry
  no `topics` or `place_info`, so topics come from unfiltered first pages.

### Step 2: collect for all restaurants (`collect_washroom_reviews.py`)

```powershell
python scripts/collect_washroom_reviews.py --max-credits 50
```

| Option | Default | Description |
|---|---|---|
| `--max-credits N` | `20` | Search budget for this run, clamped to the credits left on the account (free check) |
| `--max-pages-per-search N` | `3` | Max pages of 20 reviews per search |
| `--refresh` | off | Fetch completed restaurants again, from page 1 |
| `--limit N` | all | Only the first N restaurants of the CSV |
| `--csv PATH` | route CSV | Restaurant list with a `data_id` column |
| `--verbose` | off | Show the full per-page extraction output |

- Per restaurant: if its saved topics include a washroom-like one (matched with
  the step 3 keyword rules), that `topic_id` is used (1 search). Otherwise it
  makes 4 searches: `query` = washroom, restroom, toilet, bathroom. A restaurant
  keeps the plan it started with.
- Breadth first: page 1 of every search for every restaurant, then page 2, and
  so on while budget remains. Each page is saved straight away.
- If the budget or the credits run out, the run stops cleanly. The next run
  resumes from the saved `next_page_token`s and skips restaurants that are
  already complete. The end of the run lists the restaurants that still need
  fetching, also saved in `data/output/washroom/collection_summary.json`.
- An API error on one search is recorded and the run moves on. The search is
  retried on the next run.
- **Coverage limit:** Google's in-review search (`query`) returns only a
  subset of the matching reviews, with no further pages. For Big Bay, the 98
  newest unfiltered reviews held 57 washroom reviews, but the four searches
  together returned 22. A restaurant with 0 washroom reviews may simply be
  under-covered, so treat the counts as a lower bound. Only fetching the
  unfiltered reviews and matching them locally gives full coverage.

### Merging the sources (`merge_reviews.py`, 0 credits)

The reviews come from three sources, kept in separate folders as provenance:

| Folder | Source tag | What |
|---|---|---|
| `data/output/<data_id>_reviews.json` | `serpapi` | SerpApi unfiltered downloads |
| `data/output/washroom/` | `serpapi_washroom_search` | SerpApi washroom searches |
| `data/output/apify/` | `apify` | Apify unfiltered downloads |

`data/output/archive/` holds files that are no longer read: copies under old
file names whose reviews are all in the current files, a duplicate, and test
runs for places not in the restaurant list.

`merge_reviews.py` turns the three folders into **one dataset**:

- `data/processed/reviews/<data_id>_reviews.json`: one file per listed
  restaurant, with counts per source.
- `data/processed/all_reviews.csv`: one row per review, without reviewer
  names.

How the merge works:

- **Each review once.** A review appears once per restaurant, keyed by its
  Google `review_id`, which is the same in SerpApi and Apify.
- **Fields.** When several sources have a review, the first source's fields
  are kept and gaps are filled from the others.
- **Provenance.** `sources` lists every source that found the review, and
  `matched_by` every washroom search that returned it.
- **Dates and order.** Dates are normalised to `YYYY-MM-DDTHH:MM:SSZ`, and
  reviews are listed newest first.

Step 3 rebuilds the merge itself before extracting, so its output can never
lag behind the sources.

### Step 3: extract washroom mentions (`extract_washroom_mentions.py`, 0 credits)

```powershell
python scripts/extract_washroom_mentions.py
```

Reads the merged dataset (rebuilt first, see above) and classifies each review as
washroom-related when its text mentions washroom, restroom, toilet, bathroom,
loo, lavatory, wash room, rest room, WC, urinal or hand wash area. Matching is:

- **Case-insensitive and whole-word.** "rested", "toiletries", "Waterloo" and
  "there was room" do not match.
- **Variant-tolerant.** It covers plurals, `wash-room`, `W.C.`, `handwash area`
  and common misspellings (washrom, wahsroom, toliet, restrom, bathrom,
  lavatry, ...). The Indian "tolet" (to-let) sign is not treated as a toilet.
- **Usage-aware.** Uses like "bathroom-themed decor" or "toilet humour" are
  ignored. A real mention in the same review still counts.

Only the washroom passage of a related review is kept:

- Sentences containing a washroom word become the snippet.
- A short follow-on sentence about it is kept with it, as in
  "Washroom is upstairs. It was dirty."
- A lead-in such as "Parking/Restroom:" or "And the restrooms?" keeps the
  sentence after it.
- Sentences also end at `…` and at a stop glued to the next word
  ("taste .Dosa was raw"), so food talk is not pulled in.
- A review with several separate washroom passages is still one row: the
  passages are joined with ` … ` and `passage_count` says how many there are.
- Long run-on sentences are trimmed to the washroom clause.
- Google's "(Original)" text after a translation is dropped.

Snippets are single-line plain text, ready to send to a sentiment API.

| File | One row per | Columns |
|---|---|---|
| `data/processed/washroom_mentions.csv` | washroom-related review (each counted once) | `mention_id` (the `review_id`), `restaurant`, `data_id`, `review_id`, `review_date`, `iso_date`, `star_rating`, `washroom_snippet` (separate washroom passages of one review joined with ` … `), `passage_count`, `full_review_text`, `review_link` |
| `data/processed/washroom_summary.csv` | restaurant (all CSV rows) | `restaurant`, `data_id`, `google_review_count`, `sample_reviews`, `sample_reviews_with_text`, `washroom_in_sample`, `washroom_share`, `washroom_search_reviews`, `washroom_related_reviews` (= its rows in `washroom_mentions.csv`), `latest_mention_date` |

Both CSVs are UTF-8 (no BOM). The summary keeps the two review sources apart:

- `washroom_search_reviews`: reviews returned by the washroom searches. Those
  searches only return reviews that match, so they are washroom reviews by
  construction and say nothing about how common washroom talk is.
- `sample_reviews`: the restaurant's newest N unfiltered reviews by date
  (`--sample-size`, default 100). The same definition applies to every
  restaurant. Older downloads in Google's "most relevant" order favour long
  reviews, which mention washrooms far more often, so they are not used for
  the share.
- `sample_reviews_with_text` / `washroom_in_sample`: how many of those
  reviews have text (many are star ratings only), and how many of those are
  about the washroom. `washroom_share` is `washroom_in_sample` divided by
  `sample_reviews_with_text`. It is empty when the restaurant has no
  unfiltered reviews.
- `washroom_related_reviews`: all unique washroom reviews from both sources.
  A review found both ways counts once.
- `google_review_count`: Google's total from the restaurant list, for scale.

---

### Step 4: washroom sentiment (`score_washroom_sentiment.py`, local, free)

```powershell
python scripts/score_washroom_sentiment.py
```

Scores every washroom mention on your own computer with the aspect-based
sentiment model `yangheng/deberta-v3-base-absa-v1.1`. It needs no API key and
uses no credits.

- **About the washroom, not the visit.** The model rates the sentiment towards
  one aspect, so "Food was amazing, but the washroom was filthy" scores
  negative. Each snippet is scored with the washroom word the reviewer used
  ("restroom", "toilet", ...) as the aspect. A review with several passages is
  scored passage by passage, and the results are averaged.
- **Score.** `sentiment_score` is P(positive) minus P(negative), from -1 to +1.
- **Model download.** The model (~740 MB) downloads once into the Hugging Face
  cache in your user folder, never into the repository. On a laptop CPU the
  372 mentions take about a minute.
- **Checked on this data.** Negative washroom reviews average 2.4 stars,
  neutral 3.3 and positive 4.5, and the score correlates 0.63 with the star
  rating. Most of the remaining mismatches are reviews whose washroom opinion
  differs from their overall rating.
- **`needs_review`.** This flags the scores worth a human look: the model's
  confidence is below 0.6, or a review's passages get different labels. The
  model's errors in a manual check fell into these groups.

| File | One row per | Columns |
|---|---|---|
| `data/processed/washroom_mentions_scored.csv` | washroom mention | the `washroom_mentions.csv` columns + `aspect`, `sentiment`, `sentiment_score`, `confidence`, `p_negative`, `p_neutral`, `p_positive`, `needs_review` |
| `data/processed/washroom_sentiment_summary.csv` | restaurant | `washroom_reviews`, `positive`, `neutral`, `negative`, `mean_sentiment_score`, `share_negative`, `recent_reviews` / `recent_mean_sentiment_score` (last 2 years), `needs_review`, `latest_mention_date`, `low_data` (fewer than 5 washroom reviews) |

## 9. Tests

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
`test_restaurant_photos.py` covers the main restaurant photo: lookup and
identity checks, fallback labelling, download order, gallery order, category
separation, missing photos, failed downloads, duplicates, caching and the
request budget.
`test_washroom.py` covers the `--query` / `--topic-id` parameters (`num=20`,
mutual exclusion, tags, dedupe, resume), saved topics, the collector's credit
budget, resume, skip and refresh logic, keyword matching (including false
positives such as "rested" and "bathroom-themed") and snippet extraction.
