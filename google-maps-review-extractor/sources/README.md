# sources/

This folder contains documentation and reference notes about the data sources
used by the Google Maps Review Extractor.

## Contents

| File | Description |
|------|-------------|
| `README.md` | This file — describes the folder's purpose |

## What belongs here

- Notes on the SerpApi `google_maps_reviews` engine and its limitations
- References to official SerpApi documentation
- Information about how `data_id` values are obtained from Google Maps
- Free-plan quota details and rate limit information

## What does NOT belong here

- API keys or any credentials (use `.env` for secrets)
- Scraped review data (goes in `data/` or `output/`)
- Copyrighted content reproduced without permission

## Finding a restaurant's data_id

1. Open [Google Maps](https://maps.google.com) and search for the restaurant.
2. Click on the restaurant to open its detail panel.
3. Copy the URL from the address bar — it contains the CID or place identifier.
4. Alternatively, use the SerpApi [Google Maps Search](https://serpapi.com/google-maps-api)
   engine to retrieve the `data_id` for a given place name and location.

Example `data_id` format:
```
0x3bb1e3f2fe781a35:0xa978b861803d2fbb
```

## SerpApi documentation

- API Overview: https://serpapi.com/google-maps-reviews-api
- Pagination: Use the `next_page_token` from `serpapi_pagination` in each response
- Free plan: ~100 searches/month; each paginated page counts as one search
