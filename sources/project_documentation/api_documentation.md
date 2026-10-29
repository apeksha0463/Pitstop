# API Documentation

Reference notes for SerpApi's `google_maps_reviews` engine as used by Pitstop.
Official docs: <https://serpapi.com/google-maps-reviews-api>

## SerpApi Google Maps Reviews Engine

- Endpoint: `https://serpapi.com/search.json`
- Engine: `google_maps_reviews`
- Key parameter: `data_id` (Google Maps feature identifier, e.g. `0x3bae19cd83892495:0xc4ee26c182749dfe`)
- Language: `hl=en`
- Sorting: `sort_by` = `qualityScore` (default) | `newestFirst` | `ratingHigh` | `ratingLow`. Pitstop uses `newestFirst`.
- Page size: `num` = 1–20 (default 10). Pitstop sends `num=20` only on requests that also carry `next_page_token`; the first page uses the default.
- Pagination token: `serpapi_pagination.next_page_token`. No token means no further page.
- Quota: 1 search credit per page request.

## Response fields used

- `place_info`: `title`, `address`, `rating`, `reviews` (total count reported by Google), `type`
- `reviews[]`: `review_id`, `user` (`name`, `link`, `thumbnail`, `local_guide`, `reviews`, `photos`),
  `rating`, `date`, `iso_date`, `iso_date_of_last_edit`, `snippet`, `extracted_snippet`,
  `details`, `likes`, `link`, and **`images`**
- `reviews[].images`: an array of image URL strings (`https://lh3.googleusercontent.com/...`)
  attached to that review. It is absent when the review has no images. `user.thumbnail` is the reviewer's
  avatar, not a review image.

## Account API (key validation)

- Endpoint: `https://serpapi.com/account.json?api_key=...`
- Free: does not consume search credits.
- Returns `plan_name`, `searches_per_month`, `plan_searches_left`, `total_searches_left`, etc.
- An invalid key returns an `error` field / HTTP 401.

## Error handling

- HTTP 401 or `"Invalid API key"`: invalid or revoked key
- HTTP 429 with `"run out of searches"`: credits exhausted; other 429s are hourly rate limits
- HTTP 200 with `"Google hasn't returned any results"`: treated as an empty page
