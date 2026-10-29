# API Documentation

Reference notes for SerpApi's `google_maps_reviews` engine as used by Pitstop.
Official docs: <https://serpapi.com/google-maps-reviews-api>

## SerpApi Google Maps Reviews Engine

- Endpoint: `https://serpapi.com/search.json`
- Engine: `google_maps_reviews`
- Key parameter: `data_id` (Google Maps feature identifier, e.g. `0x3bae19cd83892495:0xc4ee26c182749dfe`)
- Language: `hl=en`
- Sorting: `sort_by` = `qualityScore` (default) | `newestFirst` | `ratingHigh` | `ratingLow`. Pitstop uses `newestFirst`.
- Page size: `num` = 1–20 (default 10). Pitstop sends `num=20` on requests that carry `next_page_token`, and also on page 1 when `query` or `topic_id` is set; an unfiltered first page uses the default.
- Filters: `query` (text search within the reviews) and `topic_id` (a topic `id` from the response's `topics`). They can't be used together. Filtered responses observed so far contain no `place_info` and no `topics`; with no match, `search_information.reviews_results_state` is `"No reviews"`.
- `topics[]` (unfiltered page 1): `{keyword, mentions, id}`; `id` is what `topic_id` takes.
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

## SerpApi Google Maps Place Results (main restaurant photo)

Official docs: <https://serpapi.com/google-maps-api>, <https://serpapi.com/maps-place-results>

- Engine: `google_maps`. Pitstop sends `data_cid` (usable on its own, with no `type` or `ll` needed) and `hl=en`.
- `data_cid` is the decimal value of the second hex part of the `data_id`
  (`0x...:0xc4ee26c182749dfe` -> `int("c4ee26c182749dfe", 16)`).
- Response `place_results`: `title`, `data_id`, `data_cid`, `place_id`, `address`, `rating`, `type`,
  **`thumbnail`** (the listing's primary image, used as the main restaurant photo) and
  **`images[]`** (`{title, thumbnail}`, one per photo category such as "All", "Menu" or "Latest").
- Identity check: `place_results.data_id` (or `data_cid`) must equal the requested restaurant.
- Quota: 1 search credit. Cached in `data/raw_data/serpapi/<data_id>_place.json`.

## SerpApi Google Maps Photos API (optional)

Official docs: <https://serpapi.com/google-maps-photos-api>

- Engine: `google_maps_photos`, `data_id`, optional `category_id`, `next_page_token` (20 photos per page), `hl`.
- The docs do not say which photo is primary, so Pitstop never takes the main photo from it.
  It is used only for extra restaurant photos with `--restaurant-photo-pages N` (1 credit per page).

## Account API (key validation)

- Endpoint: `https://serpapi.com/account.json?api_key=...`
- Free: does not consume search credits.
- Returns `plan_name`, `searches_per_month`, `plan_searches_left`, `total_searches_left`, etc.
- An invalid key returns an `error` field / HTTP 401.

## Error handling

- HTTP 401 or `"Invalid API key"`: invalid or revoked key
- HTTP 429 with `"run out of searches"`: credits exhausted; other 429s are hourly rate limits
- HTTP 200 with `"Google hasn't returned any results"`: treated as an empty page
