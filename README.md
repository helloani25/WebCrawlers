## NJMLS Spider

The NJMLS spider extracts active residential listings from the New Jersey Multiple Listing Service public search portal, crawling all 21 NJ counties.

### Current State (2026-05-18)

- Crawl breadth: all NJ counties, city-level sharding from `xhr.multiple_town_select_new`.
- Detail enrichment: follows `dsp.info` per listing and merges detail fields.
- Key extracted fields now include:
  - `property_remarks`
  - `tax_annual_amount`, `tax_year`
  - `days_on_market`
  - `photo_links`, `photos_count`, `first_photo_url`
  - listing agent/office contact fields (`listing_agent*`, `listing_office*`)
- Parser behavior: detail values backfill missing card values for `property_type`, `sqft`, remarks, and photos.

Smoke test:

```bash
scrapy crawl njmls -a max_counties=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

### TODO

- Add optional community enrichment mode to NJMLS spider (`include_community_info=1`).
- Queue properties for community extraction in SQLite (durable restartable queue): `pending -> in_progress -> done/failed`.
- For each queued property, call community endpoint: `/communities/index.cfm?action=dsp.towninfo&townname=<TOWN>&view=facts&mlsnum=<MLS>&county=<COUNTY>`.
- Amenities example link: `https://www.njmls.com/communities/index.cfm?action=dsp.towninfo&townname=SADDLE%20BROOK&view=amenities&mlsnum=26015947&county=BERGEN`.
- Parse and normalize community fields: `community_demographics`, `community_schools`, `community_amenities`, `community_public_transit`.
- For amenities, capture a nearby + popular mix for practical categories: banks, restaurants, gas stations, pharmacies, groceries, hospitals.
- Enforce extraction limits for amenities (top 20 entities per category).
- Later add property tax history and sale history extraction.
- Exclude disclaimer and weather text from community output.

### TODO
I want to consolidate the naming convention for the properties across all the spiders - zillow, gsmls, redfin, realtor, bhgre, weichert, remax, njmls
Need a way to use Items.py for all the property scraping spiders.

### How to Run

```commandline
export PYTHONPATH=$PYTHONPATH:$(pwd)
scrapy crawl njmls -o njmls_output.csv
```

### Canonical Schema Backfill (MongoDB)

Backfill historical documents with canonical property fields (`canonical_schema_version=6`):

```bash
python scrapy_crawlers/scripts/backfill_canonical_fields.py --dry-run
python scrapy_crawlers/scripts/backfill_canonical_fields.py --collections redfin,remax,bhgre,gsmls,njmls,weichert,realtor,zillow
```

After schema updates (for example adding `full_baths`, `half_baths`, `living_area_sqft`,
`days_on_market`, `tax_annual_amount`, `garage_spaces`, `heating`, `cooling`), run:

```bash
python scrapy_crawlers/scripts/backfill_canonical_fields.py --dry-run --force-all
python scrapy_crawlers/scripts/backfill_canonical_fields.py --collections redfin,remax,bhgre,gsmls,njmls,weichert,realtor,zillow --force-all
```

### Current State Snapshot (2026-05-18)

This is the current implementation status for the active NJ crawlers:

- `njmls`: county + city sharding from town modal, detail-page follow-up enabled, photos/tax/DOM/remarks extraction enabled.
- `zillow`: city-first search strategy, county fallback, then statewide bounding-box fallback; strategy counters logged at spider close.
- `bhgre`: paginated listings API + per-listing detail enrichment; normalized flat `mls_id`, listing contact fields, multi-photo extraction.
- `gsmls`: county/town flow with over-250 result splitting, detail-page enrichment, broader field extraction and style normalization.

Recommended post-change backfill:

```bash
python scrapy_crawlers/scripts/backfill_canonical_fields.py --dry-run --collections zillow,gsmls,njmls,bhgre --force-all
python scrapy_crawlers/scripts/backfill_canonical_fields.py --collections zillow,gsmls,njmls,bhgre --force-all
```

### Tiered Anti-Bot Strategy (DataImpulse + Scrapfly ASP)

Current recommended control plane for blocked/detail-prone sources (especially `remax`, `njmls`):

1. Primary path: run standard spider traffic through DataImpulse rotating residential proxy.
2. Fallback path: only when detail response is blocked/non-200/empty, retry that detail URL through Scrapfly ASP.
3. Emit item even when fallback fails, with parse-status markers for backfill/re-crawl targeting.

Why this tiered approach is better than always-on ASP:

- Lower cost: ASP is paid only on blocked pages, not on every request.
- Better throughput: normal pages avoid browser-grade anti-bot overhead.
- Better diagnostics: blocked vs recovered traffic is visible via `detail_parse_status`.
- Cleaner recovery loops: rerun/backfill can target only `non_200_*`, `blocked_*`, `scrapfly_non_200_*` rows.

Important:

- Do not chain both providers on the same request path.
- Use DataImpulse as primary proxy for spider requests.
- Use Scrapfly ASP only as conditional fallback request.

#### Setup

DataImpulse (primary):

```bash
export PROXY_USERNAME='YOUR_DATAIMPULSE_USERNAME'
export PROXY_PASSWORD='YOUR_DATAIMPULSE_PASSWORD'
export PROXY_HOST='gw.dataimpulse.com'
export PROXY_PORT='823'
export PROXY_PARAMS='rotating=true'
```

Scrapfly ASP (fallback):

```bash
export SCRAPFLY_API_KEY='YOUR_SCRAPFLY_TEST_OR_PROD_KEY'
export SCRAPFLY_ASP_ENABLED='1'
export SCRAPFLY_PROXY_POOL='public_residential_pool'
export SCRAPFLY_COUNTRY='us'
export SCRAPFLY_RENDER_JS='0'
```

Per-spider override flags are also supported:

- `REMAX_SCRAPFLY_ASP_ENABLED=1`
- `NJMLS_SCRAPFLY_ASP_ENABLED=1`

#### Smoke Tests

RE/MAX:

```bash
scrapy crawl remax -a state=nj -a max_pages=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

NJMLS:

```bash
scrapy crawl njmls -a max_counties=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

Debug log checks for fallback:

```bash
grep -E "retrying with Scrapfly ASP|scrapfly_ok|scrapfly_non_200|blocked_202_after_retries|detail_parse_status" -n logs/*.log
```

#### Logging Details

Capture DEBUG logs to file:

```bash
mkdir -p logs
scrapy crawl remax -a state=nj -a max_pages=1 -s LOG_LEVEL=DEBUG -s LOG_FILE=logs/remax_debug.log -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
scrapy crawl njmls -a max_counties=1 -s LOG_LEVEL=DEBUG -s LOG_FILE=logs/njmls_debug.log -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

Key fallback traces:

- `RE/MAX detail retrying with Scrapfly ASP: status=... mls_id=... url=...`
- `NJMLS detail retrying with Scrapfly ASP: status=... mls_id=... url=...`
- `detail_parse_status=scrapfly_ok`
- `detail_parse_status=scrapfly_non_200_*`

Quick counters from logs:

```bash
grep -Eo "detail_parse_status['=: ]+[A-Za-z0-9_]+" logs/remax_debug.log logs/njmls_debug.log | sort | uniq -c
grep -E "retrying with Scrapfly ASP" -n logs/remax_debug.log logs/njmls_debug.log | head -50
```

What to alert on:

- no `retrying with Scrapfly ASP` lines when blocked statuses are present
- high `scrapfly_non_200_*` rate
- repeated `scrapfly_empty_body` on the same template URL pattern

#### Runbook: Detail Parse Status

| `detail_parse_status` pattern | Meaning | Action |
|---|---|---|
| `ok` | Detail parsed via primary path | No action |
| `scrapfly_ok` | Primary blocked, ASP fallback recovered detail | Keep current settings; monitor fallback rate |
| `non_200_403` / `non_200_429` | Primary blocked and no ASP recovery | Verify proxy health, enable ASP, lower concurrency/delay |
| `non_200_202` / `blocked_202_after_retries` | Edge challenge persisted on detail page | Keep retries/session rotation enabled; ensure ASP fallback is on |
| `scrapfly_non_200_*` | ASP request also blocked/failed upstream | Verify Scrapfly key/quota, switch pool/country, retry later |
| `empty_body` / `scrapfly_empty_body` | Response succeeded but returned no parseable body | Re-run sample with debug logs; validate page template changed |

Operational thresholds:

- If `scrapfly_ok` is high but stable, keep tiered mode (primary + fallback).
- If `scrapfly_non_200_*` exceeds ~10% of detail attempts, investigate ASP account/pool health immediately.
- Re-crawl/backfill only failed rows using `detail_parse_status` filters instead of full recrawls.

### GCS Image Sync (Deduplicated)

Use `scrapy_crawlers/scripts/sync_listing_images_to_gcs.py` to pull listing images from Mongo and upload them to GCS with SHA-256 dedupe, chunked concurrent fetch workers, stage/progress logging, and SQLite resume support.

Core behavior:

- Source image inputs per listing:
  - `photo_links`
  - `first_photo_url` (canonical first-image field)
  - `primary_photo_url` (legacy top-level fallback for older rows)
  - `source_fields.first_photo_url` (backward-compatible fallback)
  - `source_fields.primary_photo_url` (legacy fallback)
- Dedupe model:
  - content hash (`sha256`) is canonical duplicate key
  - if hash exists in `image_assets`, upload is skipped and existing `gcs_uri` is reused
- Canonical object path:
  - `<GCS_IMAGE_PREFIX>/sha256/<first2>/<sha256>.<ext>`
- Listing writes:
  - `gcs_images[]` with `index`, `source_url`, `sha256`, `gcs_uri`
  - `gcs_images_count`
  - `gcs_images_synced_at`
- Dedupe index collection:
  - `image_assets` (`sha256` unique, `gcs_uri` unique sparse)

Resume queue behavior (SQLite):

- Queue DB stores listings by `(collection_name, doc_id)` with statuses:
  - `pending -> in_progress -> done` or `failed`
- On restart, all `in_progress` rows are automatically moved back to `pending`.
- Queue is rebuilt from Mongo using `INSERT OR IGNORE` so already-queued/processed rows are preserved.
- Optional controls:
  - `--reset-queue`: clear queue for selected collections and rebuild
  - `--retry-failed`: move failed rows back to pending
  - recommended after transient network/provider issues to reprocess only failed queue entries

Required env vars:

- `GOOGLE_APPLICATION_CREDENTIALS` (relative path such as `photo_credentials.json` is resolved from repo root and `scrapy_crawlers/settings`)
- `GCP_PROJECT_ID`
- `GCS_IMAGE_BUCKET`

Optional env vars:

- `GCS_BUCKET_LOCATION` (default `US`)
- `GCS_IMAGE_PREFIX` (default `property-images`)
- `IMAGE_SYNC_COLLECTIONS` (default `njmls,remax,weichert,redfin,realtor,bhgre,gsmls,zillow`)
- `IMAGE_SYNC_MAX_IMAGES_PER_LISTING` (default `30`)
- `IMAGE_SYNC_WORKERS` (default `8`)
- `IMAGE_SYNC_CHUNK_SIZE` (default `100`)
- `IMAGE_SYNC_PROGRESS_EVERY_DOCS` (default `50`)
- `IMAGE_SYNC_RESUME_DB` (default `scrapy_crawlers/scripts/image_sync_resume.sqlite3`)
- `IMAGE_SYNC_QUEUE_BATCH_SIZE` (default `200`)

CLI options:

| Option | Description | Default |
|---|---|---|
| `--collections` | Comma-separated Mongo collections | `njmls,remax,weichert,redfin,realtor,bhgre,gsmls,zillow` |
| `--limit-listings` | Max listings per collection (`0` = no limit) | `0` |
| `--max-images-per-listing` | Max image URLs processed per listing | `30` |
| `--create-bucket` | Create GCS bucket if missing | `false` |
| `--dry-run` | Skip uploads and Mongo writes | `false` |
| `--workers` | Concurrent threads for fetch + fingerprint | `8` |
| `--chunk-size` | Photo URLs processed per worker chunk | `100` |
| `--progress-every-docs` | Emit progress every N processed listings | `50` |
| `--resume-db` | SQLite queue file path | `scrapy_crawlers/scripts/image_sync_resume.sqlite3` |
| `--queue-batch-size` | Pending queue rows dequeued per batch | `200` |
| `--reset-queue` | Clear queue rows for selected collections before rebuild | `false` |
| `--retry-failed` | Move `failed` rows back to `pending` before run | `false` |

Commands:

```bash
pip install -r requirements.txt
```

```bash
# Preflight config + auth + queue build using one listing (no writes)
python scrapy_crawlers/scripts/sync_listing_images_to_gcs.py --dry-run --limit-listings 1
```

```bash
# Small smoke sync with controlled concurrency
python scrapy_crawlers/scripts/sync_listing_images_to_gcs.py --create-bucket --limit-listings 20 --workers 8 --chunk-size 100
```

```bash
# Full resumable sync (all configured collections)
python scrapy_crawlers/scripts/sync_listing_images_to_gcs.py
```

```bash
# Resume run and retry previously failed queue rows
python scrapy_crawlers/scripts/sync_listing_images_to_gcs.py --retry-failed
```

```bash
# Rebuild queue for selected collections from scratch
python scrapy_crawlers/scripts/sync_listing_images_to_gcs.py --collections remax,njmls,bhgre --reset-queue --limit-listings 500
```

Limit to a subset of counties for testing:

```bash
scrapy crawl njmls -a max_counties=2 -o njmls_sample.csv
```

Run without a proxy:

```bash
scrapy crawl njmls -a disable_proxy=1 -o njmls_output.csv
```

### Spider Flow

```
homepage  →  dsp.search page  →  xhr.multiple_town_select_new  (city/town iframe)
                                             │
                                Parse all city checkboxes per county
                                             │
                        For every (county, city) pair, build one search URL:
                        /listings/index.cfm?action=xhr.results.view.rerunphoto
                                            &county=bergen&city=HACKENSACK
                                            &page=1&display=30&countysearch=true&status=A
                                             │
                                  Paginate page=2, 3, … until < 30 results returned
```

1. **Cookie Priming:** Opens the NJMLS homepage to establish a valid session and acquire cookies.
2. **Search Page:** Loads `dsp.search` to obtain the link to the town-selection modal iframe (`xhr.multiple_town_select_new`).
3. **City-Level Sharding:** Fetches the town modal iframe once. The iframe contains checkbox inputs, one per city/town, with values in the format `HACKENSACK, NJ, BERGEN, 07601`. The spider parses every city per county and issues one search request per `(county, city)` pair. This avoids hitting NJMLS's implicit result cap that can truncate county-wide searches.
4. **Pagination:** Each city shard fetches 30 results per page. The spider increments `page` until a page returns fewer than 30 listings.
5. **Deduplication:** MLS IDs are deduplicated globally across all county/city shards to prevent duplicate rows.

#### City Checkbox Parsing (`_extract_city_values_for_county`)

The town modal returns checkboxes with `value` attributes in this format:
```
HACKENSACK, NJ, BERGEN, 07601
```
The spider splits on `,`, takes `parts[0]` (city name) and matches `parts[2]` (county token) against the target county to collect all cities for that county.

Three fallback strategies are tried in order:
1. Checkbox inputs grouped under an `<h3>BERGEN COUNTY, NJ</h3>` heading (current page format).
2. Flat checkbox list filtered by the county token in `parts[2]` (older format).
3. `<option>` values from a legacy dropdown (oldest format).
4. County-wide search with `city=""` if the modal is unavailable or returns no matches.

### HAR Analysis (`~/Downloads/www.njmls.com.har`)

#### 1. Extracting NJMLS Endpoints

```bash
jq -r '.log.entries[] | select(.request.url | contains("njmls.com")) | .request.url' \
  ~/Downloads/www.njmls.com.har | sort -u
```

Key endpoints discovered:
- `GET https://www.njmls.com/` — homepage / cookie priming
- `GET https://www.njmls.com/listings/index.cfm?action=dsp.search` — search form page (contains link to town modal)
- `GET https://www.njmls.com/listings/views/xhr.cookieset.cfm` — search cookie init
- `GET https://www.njmls.com/listings/index.cfm?action=xhr.multiple_town_select_new` — city/town checkbox iframe (one city per county, used for sharding)
- `GET https://www.njmls.com/listings/index.cfm?action=xhr.results.view.rerunphoto` — paginated listing results (HTML fragment)
- `GET https://www.njmls.com/listings/index.cfm?action=dsp.info&mlsnum=<MLSNUM>` — individual listing detail page

#### 2. Inspecting the Results Request

```bash
jq -r '.log.entries[]
  | select(.request.url | contains("xhr.results.view.rerunphoto"))
  | .request.url' \
  ~/Downloads/www.njmls.com.har | head -5
```

Example output:
```text
https://www.njmls.com/listings/index.cfm?action=xhr.results.view.rerunphoto&page=1&display=30&sortBy=newest&state=NJ&county=bergen&countysearch=true&status=A&...
https://www.njmls.com/listings/index.cfm?action=xhr.results.view.rerunphoto&page=2&display=30&sortBy=newest&state=NJ&county=bergen&countysearch=true&status=A&...
https://www.njmls.com/listings/index.cfm?action=xhr.results.view.rerunphoto&page=3&display=30&sortBy=newest&state=NJ&county=bergen&countysearch=true&status=A&...
```

Key query parameters:
- `action=xhr.results.view.rerunphoto` — returns an HTML fragment with listing cards
- `page=N` — 1-based page number
- `display=30` — results per page
- `county=<name>` — lowercase county name (e.g., `bergen`, `cape may`)
- `countysearch=true` — county-scoped search
- `state=NJ`, `status=A` — active NJ listings only
- `X-Requested-With: XMLHttpRequest` — required header (AJAX endpoint)

#### 3. Inspecting Request Headers

```bash
jq '.log.entries[]
  | select(.request.url | contains("xhr.results.view.rerunphoto"))
  | .request.headers[]
  | select(.name | test("^(Accept|X-Requested|Referer|sec-ch)"; "i"))' \
  ~/Downloads/www.njmls.com.har | head -40
```

Required headers identified:
- `Accept: text/html, */*; q=0.01`
- `X-Requested-With: XMLHttpRequest`
- `Referer: https://www.njmls.com/<county>-county-nj-property`

#### 4. Property Detail URL Pattern

From Google Analytics page titles captured in the HAR:
```text
MLS Number 25025583 - 4 bed,1 bath, Residential Property for $655,000 - 1330 Stockton Street, Rahway, NJ
MLS Number 26017619 - 4 bed,3 bath, Residential Property for $999,999 - 347 Passaic Avenue, Rutherford, NJ
```

Detail page URL format:
```text
https://www.njmls.com/listings/index.cfm?action=dsp.info&mlsnum=<MLSNUM>&proptype=1,2,3
```

MLS numbers are 8-digit integers (e.g., `25025583`, `26017619`).

### NJ Counties Crawled

All 21 NJ counties: atlantic, bergen, burlington, camden, cape may, cumberland, essex, gloucester, hudson, hunterdon, mercer, middlesex, monmouth, morris, ocean, passaic, salem, somerset, sussex, union, warren.

### Output Fields

Each item includes:

- `source` — always `"njmls"`
- `mls_id` — 8-digit MLS listing number
- `detail_url` — full URL to the listing detail page
- `address` — street address
- `city`, `state`, `postal_code` — location components
- `county` — NJ county name (lowercase)
- `list_price` — listing price (integer)
- `beds`, `baths` — bedroom and bathroom counts
- `sqft` — square footage
- `property_type` — property type (Single Family, Condo, etc.)
- `status` — always `"ACTIVE"` for this spider

### Parser Notes

NJMLS serves HTML fragments (server-rendered CFML). The spider uses ordered XPath fallbacks anchored to class names, `data-mlsnum` attributes, and `dsp.info` href patterns. If a listing card lacks a detectable MLS ID and address, the card is skipped. Cards with duplicate MLS IDs across county shards are globally deduplicated.

To debug the HTML structure returned by the results endpoint:

```bash
jq -r '.log.entries[]
  | select(.request.url | contains("xhr.results.view.rerunphoto"))
  | .response.content.text' \
  ~/Downloads/www.njmls.com.har | head -c 3000
```

### Rendering Strategy — No Browser Required

NJMLS is **not** a React/SPA site. It uses a **ColdFusion (CFML) + jQuery AJAX** stack, identifiable by the `.cfm` file extensions on every endpoint:

```
/listings/index.cfm?action=xhr.results.view.rerunphoto
/listings/views/xhr.cookieset.cfm
/portfolio/index.cfm?action=xhr.isloggedin
```

| Pattern | How it works |
|---|---|
| React / Vue SPA | Browser loads a near-empty HTML shell; JavaScript fetches data and renders the DOM client-side |
| CFML + jQuery AJAX | Server generates a fully rendered HTML fragment; JavaScript inserts it into the page |

NJMLS uses the second pattern. The `action=xhr.results.view.rerunphoto` endpoint is a ColdFusion handler that returns a **pre-rendered HTML fragment** in response to the XHR call. All rendering happens on the server — the browser just receives finished HTML.

Scrapy can call that endpoint directly with the right headers (`X-Requested-With: XMLHttpRequest`) and receive fully rendered listing cards. **Playwright or browser-stealth is not needed.**

You would only need browser rendering if:
- The page returned was an empty shell (e.g. `<div id="root">`) that React fills in via client-side JS
- MLS numbers and prices were injected by JavaScript *after* the XHR response arrived

Since the XHR response itself is the server-generated HTML, plain Scrapy requests with cookie priming are sufficient.

### Environment Settings

Optional settings (`.env` or shell):

- `NJMLS_MAX_COUNTIES` — limit counties crawled (useful for testing)
- `NJMLS_SCRAPFLY_ASP_ENABLED` — enable ASP fallback for blocked detail pages (`1|true|yes`)
- `SCRAPFLY_API_KEY` (or `SCRAPFLY_KEY`) — API key for ASP fallback
- `SCRAPFLY_PROXY_POOL` (default `public_residential_pool`)
- `SCRAPFLY_COUNTRY` (default `us`)
- `SCRAPFLY_RENDER_JS` (default `0`)

### Debugging (Spider-Relevant)

Smoke test:

```bash
scrapy crawl njmls -a max_counties=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

What to verify:

- town modal returns city values per county
- each `(county, city)` shard returns cards and paginates until short page
- detail follow-up fills `property_remarks`, `tax_annual_amount`, `tax_year`, `days_on_market`
- photo fields (`photo_links`, `photos_count`, `first_photo_url`) are populated when images exist
- blocked details trigger Scrapfly ASP fallback when enabled

Fallback debug grep:

```bash
grep -E "NJMLS detail retrying with Scrapfly ASP|scrapfly_ok|scrapfly_non_200|detail_parse_status" -n logs/njmls*.log
```

---

## RE/MAX Spider

The RE/MAX spider extracts for-sale listings from `www.remax.com` using the same Next.js RSC (`_rsc`) listing endpoint pattern captured in the HAR.

### Current State (2026-05-18)

- Search breadth comes from paginated listing pages (`/homes-for-sale/<state>?searchQuery=...&_rsc=...`).
- Every card listing is followed to its detail page for enrichment.
- Detail extraction now includes many labeled fields when present (taxes, lot, parking, heating/cooling, agent/office, days on website, etc.).
- Built-in anti-block handling:
  - retries for listing pages returning `202`
  - rotating proxy session after configured intervals and on `202`
  - detail retry URL mutation (`/luxury/` and cache-busting query params) before final fallback
  - conditional Scrapfly ASP retry on blocked/non-200 detail responses
- Partial detail records are still emitted with `detail_parse_status=blocked_202_after_retries` so runs do not fail hard.

### How to Run

From `scrapy_crawlers`:

```bash
scrapy crawl remax -a state=nj -a max_pages=50 -a disable_proxy=1 -o remax_output.csv
```

Or from repo root:

```bash
export PYTHONPATH=$PYTHONPATH:$(pwd)
scrapy crawl remax -a state=nj -a max_pages=50 -a disable_proxy=1 -o remax_output.csv
```

### Supported Spider Args

- `state` (default: `nj`)  
  State slug in URL path, e.g. `nj`, `pa`, `tx`.
- `start_page` (default: `1`)  
  First `pageNumber` to request.
- `max_pages` (default: `500`)  
  Hard cap on pagination.
- `rsc_token` (default: `1`)  
  `_rsc` query token. HAR captured `1t5u4`; both can work depending on session/build.
- `disable_proxy` (`1|true|yes`)  
  Disable configured proxy for local testing.

### Environment Variables

Optional (`.env` or shell):

- `REMAX_STATE`
- `REMAX_START_PAGE`
- `REMAX_MAX_PAGES`
- `REMAX_RSC_TOKEN`
- `REMAX_SCRAPFLY_ASP_ENABLED` — enable ASP fallback for blocked detail pages (`1|true|yes`)
- `SCRAPFLY_API_KEY` (or `SCRAPFLY_KEY`) — API key for ASP fallback
- `SCRAPFLY_PROXY_POOL` (default `public_residential_pool`)
- `SCRAPFLY_COUNTRY` (default `us`)
- `SCRAPFLY_RENDER_JS` (default `0`)

### Spider Flow

1. Build RE/MAX listing URL:
   - `/homes-for-sale/<state>?searchQuery=...&_rsc=<token>`
2. Send RSC request headers:
   - `rsc: 1`
   - `next-router-state-tree: ...`
   - browser-like `sec-ch-*`, `sec-fetch-*`, `user-agent`, etc.
3. Parse listing payload from response:
   - primary: `<script type="application/ld+json">` `CollectionPage`
   - fallback: scan response text for `{"@context":"https://schema.org","@type":"CollectionPage"...}`
4. Extract `mainEntity.itemListElement` listings.
5. Normalize fields and dedupe globally by `mls_id` (fallback `detail_url`).
6. Paginate `pageNumber + 1` until:
   - no items,
   - no new deduped listings on page, or
   - `max_pages` reached.

### Rendering Strategy — Mostly No Browser, But Edge Blocking Exists

RE/MAX listing pages are delivered via Next.js RSC endpoints and include structured data the spider can parse without a browser renderer.

- Listing discovery path: RSC response + embedded structured payload
- Detail enrichment path: HTML/serialized data on property detail pages

So extraction itself is non-browser. The challenge is not rendering; it is edge bot protection on detail requests (frequent `202` challenge responses).

### Output Fields

Each item includes:

- `source` — always `"remax"`
- `mls_id`
- `detail_url`
- `address`
- `city`
- `state`
- `postal_code`
- `county` (currently `None` from this endpoint)
- `list_price`
- `beds`
- `baths`
- `sqft`
- `property_type` (schema.org type such as `SingleFamilyResidence`, `Apartment`)
- `status` (`ACTIVE`/`SOLD` when available)
- `page` (source page number)

### HAR Notes (`~/Downloads/www.remax.com.har`)

Useful verification commands:

```bash
jq -r '.log.entries[] | select(.request.url|contains("_rsc=")) | .request.url' \
  ~/Downloads/www.remax.com.har
```

```bash
jq -r '.log.entries[] | select(.request.url|contains("_rsc=")) | .request.headers[] | .name + ": " + .value' \
  ~/Downloads/www.remax.com.har
```

Key findings used in spider implementation:

- Listing navigation hits `GET /homes-for-sale/<state>?searchQuery=...&_rsc=...`.
- Request includes `rsc: 1` and a `next-router-state-tree` header.
- Response includes `CollectionPage` JSON-LD with listing cards under:
  - `mainEntity.itemListElement[].item`

### Notes / Troubleshooting

- If pages return `202`, the spider retries that page automatically (up to 2 retries).
- If listing extraction drops to zero, refresh `rsc_token` from a new HAR capture.
- Keep concurrency conservative first; RE/MAX anti-bot controls can tighten with aggressive settings.

### Cloudflare/CloudFront Blocking and Mitigation

In current runs, RE/MAX detail endpoints can return `202` challenge responses from edge protection (Cloudflare/CloudFront layer behavior). This causes many enriched fields to stay `None` unless mitigated.

Mitigation steps:

1. Use DataImpulse rotating residential proxy as primary path and keep request rate conservative.
2. Keep retries enabled and rotate proxy session on `202`.
3. Use Scrapfly ASP as fallback only when detail responses are blocked/non-200.
4. Preserve browser-like request headers and periodically refresh HAR-derived request profile (`_rsc` token + router headers).
5. Run periodic backfill/re-crawl passes for rows with blocked/fallback parse statuses.
6. Monitor `detail_http_status` and `detail_parse_status` distribution per run.

Debugging checks:

```bash
scrapy crawl remax -a state=nj -a max_pages=2 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

```bash
grep -E "detail returned 202|detail_parse_status|RE/MAX page=" -n logs/remax*.log
```

Scrapfly fallback traces:

```bash
grep -E "RE/MAX detail retrying with Scrapfly ASP|scrapfly_ok|scrapfly_non_200" -n logs/remax*.log
```

---

## Redfin Spider
The Redfin spider extracts for-sale listings across all 21 New Jersey counties using county HTML pagination plus API/detail enrichment.

### Current State (2026-05-18)

- Crawl breadth comes from county pages (`/county/<region_id>/NJ/<county-slug>` and `/page-N`).
- Listing URLs are extracted from county page HTML, deduped globally, then enriched in two hops:
  1. AVM API (`/stingray/api/home/details/avm`)
  2. property detail page HTML
- This mode intentionally does not depend on GIS list API availability.
- Known blockers are handled by fallback behavior:
  - if AVM is missing/unavailable, detail page parsing still runs
  - if detail page is non-200, base record is still emitted

### How to Run
To run the Redfin spider and save the output to a CSV file:
```commandline
export PYTHONPATH=$PYTHONPATH:$(pwd)
scrapy crawl redfin -o redfin_output.csv
```

Useful spider args:
- `disable_proxy=1` - run without configured proxy
- `max_counties=<N>` - test with first N NJ counties
- `max_pages_per_county=<N>` - cap pagination per county

Example smoke run:
```bash
cd scrapy_crawlers
scrapy crawl redfin -a disable_proxy=1 -a max_counties=1 -a max_pages_per_county=2 -o redfin_test.csv
```

### Spider Flow
1. Warm up on `https://www.redfin.com/` for session/cookies.
2. Iterate NJ counties using static county -> `region_id` mapping.
3. Fetch county HTML pages and extract `/home/<id>` listing URLs.
4. Build base item from URL + county context.
5. Enrich from AVM API when `listing_id` is available.
6. Follow property detail page and merge HTML/text fallback fields.
7. Continue paging while county has next page (bounded by `max_pages_per_county`).
8. Deduplicate globally by detail URL / listing ID.

### Rendering Strategy — No Browser Required

Redfin in this spider uses:

- server-rendered county HTML pages for listing discovery
- JSON API response for AVM enrichment
- server-rendered detail HTML for additional parsing

No browser rendering is required for current extraction flow.

### Property Detail Extraction
For each Redfin listing URL, the spider extracts/normalizes:
- Core identity: `listing_id`, `mls_id`, `detail_url`, `county`, `region_id`
- Address: `address`, `city`, `state`, `postal_code`
- Pricing/status/type: `list_price`, `status`, `property_type`
- Size/layout: `beds`, `baths`, `build_area_sqft`, `lot_size_sqft`, `lot_size_acres`, `stories`
- Structural/location: `year_built`, `latitude`, `longitude`
- Additional: `description`

Primary source is county/detail parsing, with AVM as an enrichment path.

### Address and Detail Fallbacks
- Primary address source: detail URL slug + AVM payload + detail HTML/title.
- Fallback 1: derive `address/city/state/postal_code` from Redfin detail URL slug  
  (example `/NJ/City/Street-Name-07652/home/123`).
- Fallback 2: when base item still has missing address fields, request:
  - `GET /stingray/api/home/details/avm?propertyId=...&listingId=...&accessLevel=1&pageType=1`
  and merge address fields from AVM payload.
- Fallback 3: parse detail page title/JSON/text and merge remaining missing fields.

### HAR Notes (www.redfin.com.har)
Use HAR captures to validate current endpoint usage:

```bash
jq -r '.log.entries[] | .request.url | select(contains("redfin.com/stingray"))' \
  ~/Downloads/www.redfin.com.har | sort -u
```

Key enrichment endpoint:
- `https://www.redfin.com/stingray/api/home/details/avm`

Key discovery path:
- `https://www.redfin.com/county/<region_id>/NJ/<county-slug>`

If a HAR capture is from a single detail-page session, it may miss county pagination pages. Capture a county browsing session for discovery-path debugging.

### Debugging (Spider-Relevant)

Smoke test:

```bash
scrapy crawl redfin -a disable_proxy=1 -a max_counties=1 -a max_pages_per_county=2 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

What to verify:

- county page extracts non-zero `/home/` URLs
- AVM calls return parseable payload when available
- detail requests run for each listing URL
- final items include address fields even when AVM misses (URL/title fallback)

### Output Fields
Each item includes:
- `source` (`"redfin"`)
- `county`, `region_id`
- `listing_id`, `mls_id`, `detail_url`
- `address`, `city`, `state`, `postal_code`
- `list_price`, `status`, `property_type`
- `beds`, `baths`, `build_area_sqft`, `lot_size_sqft`, `lot_size_acres`
- `year_built`, `stories`
- `latitude`, `longitude`
- `description`
- `page`

## Realtor Spider
The Realtor spider extracts New Jersey for-sale listings from Realtor's GraphQL API using a HAR-derived request profile.

### Current State (2026-05-18)

- Uses county-sharded GraphQL search (`Atlantic County, NJ` ... `Warren County, NJ`) for breadth.
- Paginates each shard with `limit/offset` until reported `total` is reached.
- Uses strict header profile from HAR (`rdc-*`, `x-rdc-visitor-id`, `x-is-bot`) to avoid `400` request rejection.
- Emits normalized listing/location/price/beds/baths/media fields from GraphQL payload and dedupes by property/listing identity.

### How to Run
```commandline
export PYTHONPATH=$PYTHONPATH:$(pwd)
scrapy crawl realtor -o realtor_output.csv
```

### HAR-Driven Endpoint
HAR file: `~/Downloads/www.realtor.com.har`

Primary listing endpoint discovered in HAR:
- `POST https://www.realtor.com/frontdoor/graphql`
- operation: `ConsumerSearchQuery`
- payload alias: `home_search.properties` (from `results`)

### Key Request Requirements from HAR
Realtor GraphQL returns `400 missing client identification headers` without these:
- `rdc-ab-test-client: rdc-search-for-sale`
- `rdc-client-name: RDC_WEB_SRP_FS_PAGE`
- `rdc-client-version: 3.0.2798`
- `x-rdc-visitor-id: <uuid>`
- `x-is-bot: false`

The spider reproduces these headers and paginates with:
- `limit=42`
- `offset += count` until `offset >= total`

### High-Volume Strategy (realtor_spider.py)
For New Jersey-scale result sets, the spider now shards search by county and deduplicates globally:
- shards: `Atlantic County, NJ` through `Warren County, NJ` (21 county shards)
- full pagination inside each shard using GraphQL `count`/`total`
- cross-shard dedupe by `property_id` (fallback `listing_id`)

This avoids relying on a single broad query path (for example `/New-Jersey/pg-206`) while still crawling to the end of each shard.

### Spider Flow

1. Warm up with search landing page to align cookies/session.
2. Iterate county shards and submit `ConsumerSearchQuery` to GraphQL endpoint.
3. Parse `home_search` result set and emit normalized records.
4. Increase `offset` and continue until shard total is exhausted.
5. Deduplicate globally across all shards.

### Rendering Strategy — API-First, No Browser Required

Realtor extraction is API-first:

- Primary data source is GraphQL JSON (`/frontdoor/graphql`).
- No DOM rendering is required for listing extraction in current implementation.

### Debugging (Spider-Relevant)

Smoke test:

```bash
scrapy crawl realtor -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

HAR checks:

```bash
jq -r '.log.entries[]
  | select(.request.url == "https://www.realtor.com/frontdoor/graphql")
  | .request.postData.text
  | fromjson
  | .operationName' ~/Downloads/www.realtor.com.har | sort | uniq -c
```

What to verify:

- GraphQL responses contain `home_search` data for each county shard
- `offset` advances and stops at shard total
- non-empty `property_id`/`listing_id` and canonical URL values

### Extracting Realtor GraphQL Calls from HAR
```bash
jq -r '.log.entries[]
  | select(.request.url == "https://www.realtor.com/frontdoor/graphql")
  | .request.postData.text
  | fromjson
  | .operationName' ~/Downloads/www.realtor.com.har | sort | uniq -c
```

Inspect `ConsumerSearchQuery` variables:
```bash
jq -r '.log.entries[]
  | select(.request.url == "https://www.realtor.com/frontdoor/graphql")
  | .request.postData.text
  | fromjson
  | select(.operationName == "ConsumerSearchQuery")
  | .variables' ~/Downloads/www.realtor.com.har
```

### Output Fields (realtor_spider.py)
Each item includes:
- Listing IDs and canonical URL
- Price/status/list date
- Beds/baths/sqft/lot sqft/type/year built
- Address/city/state/ZIP/county/lat/lon
- Photo URLs and counts
- Source MLS info and advertiser/office/phone metadata
- `search_location` shard label used for that item

## Zillow Spider
The Zillow spider extracts New Jersey for-sale listings using Zillow's search state API captured from HAR.

### Current State (2026-05-18)

- Strategy order:
  1. `city` shards discovered from county pages
  2. `county` fallback when city discovery fails
  3. statewide `bbox` fallback if county scheduling is unavailable
- Oversized or blocked queries still split recursively by map bounds.
- End-of-run logs now include strategy counters for `queries`, `result_pages`, `empty_pages`, and emitted `items`.
- Zillow-specific enrichment includes:
  - `living_area_sqft`
  - `tax_assessed_value`
  - `days_on_zillow`
  - `is_preforeclosure_auction`
  - `lot_area_value`, `lot_area_unit`
  - `photo_links` built from `carouselPhotosComposable.baseUrl + photoData[].photoKey`

### How to Run
```commandline
export PYTHONPATH=$PYTHONPATH:$(pwd)
scrapy crawl zillow -o zillow_output.csv
```

### HAR-Driven Endpoint
HAR file: `~/Downloads/www.zillow.com.har`

Primary listing endpoint discovered in HAR:
- `PUT https://www.zillow.com/async-create-search-page-state`
- request body sections: `searchQueryState`, `wants`, `requestId`
- listings path: `cat1.searchResults.listResults`
- paging path: `cat1.searchList.totalPages`

### Spider Flow (zillow_spider.py)
1. Warm up on `https://www.zillow.com/nj/`.
2. Discover county browse pages from `https://www.zillow.com/browse/homes/nj/`.
3. Discover city labels from each county page and run `city` query shards.
4. If city discovery fails for a county, run `county` query fallback.
5. If county fallback cannot be scheduled, run statewide `bbox` fallback.
6. Call `PUT /async-create-search-page-state` for each query and parse `cat1.searchResults.listResults`.
7. Split oversized/blocked queries into child bounds and recurse.
8. Dedupe globally by `zpid` and paginate until `currentPage == totalPages`.

Smoke test:
```bash
scrapy crawl zillow -a max_counties=2 -a max_cities=20 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

### Rendering Strategy — Hybrid SSR Discovery + JSON Search API

Zillow extraction in this spider is split across:

- SSR/HTML browse pages for county/city discovery
- JSON API (`/async-create-search-page-state`) for listing payloads

No browser renderer is required in the current flow.

### Key Response Fields Parsed
Each item includes:
- `zpid`, `listing_id`, `pals_id`, canonical listing `url`
- `price` (numeric) and `price_display`
- beds/baths/sqft
- address, city, state, ZIP, latitude, longitude
- listing status/status text and broker name
- home metadata from `hdpData.homeInfo` (home type, days on Zillow, lot size, zestimate/rent zestimate)
- `split_depth` and `split_path` (tile lineage for traceability)

### HAR Inspection Snippets
Extract Zillow search API request payload:
```bash
jq -r '.log.entries[]
  | select(.request.url=="https://www.zillow.com/async-create-search-page-state")
  | .request.postData.text' ~/Downloads/www.zillow.com.har
```

Inspect pagination and totals from response:
```bash
jq -r '.log.entries[]
  | select(.request.url=="https://www.zillow.com/async-create-search-page-state")
  | .response.content.text' ~/Downloads/www.zillow.com.har \
  | jq '.cat1.searchList | {totalResultCount, resultsPerPage, totalPages, pagination}'
```

### Debugging (Spider-Relevant)

What to verify during runs:

- city shard discovery count per county
- county fallback activation only when city discovery fails
- bbox fallback activation only when county scheduling fails
- end-of-run strategy summary counters (`queries`, `result_pages`, `empty_pages`, `items`)

Quick log filter:

```bash
grep -E "strategy=|Using county fallback|bounding-box fallback|crawl summary" -n logs/zillow*.log
```

## Proxy Configuration
The project is configured to support rotating proxies.

### Configuration
Update `scrapy_crawlers/settings/settings.py` with your proxy provider credentials:
```python
CURL_PROXY = "http://YOUR_USERNAME:YOUR_PASSWORD@<your-proxy>.com:xxxx"
```

### How it Works
The `CurlCffiDownloadHandler` automatically detects the `CURL_PROXY` setting and routes all requests through the proxy. It also supports per-request proxies via `request.meta['proxy']`.

## Scraping using Selenium
```commandline
pip3 install chromedriver-binary-auto
pip install -U selenium
```

## Install Scrapy
```commandline
  pip install Scrapy
```

## Install Beautifulsoup
```commandline
pip3 install beautifulsoup4
```

## Install scrapy-user-agents
```commandline
pip install scrapy-user-agents
```

#### Running mysql
```markdown
docker run -d -it  -e MYSQL_ROOT_PASSWORD=pa55w0rd -e MYSQL_DATABASE=db_example --name mysql_test_db mysql
  docker images
  docker ps
  docker exec mysql_test_db -it /bin/bash
  docker exec -it mysql_test_db /bin/bash
```

TODO: Starting redis
```commandline
  redis-server
```

## Scraping
### Tutorial Project
```markdown
  scrapy startproject tutorial
  scrapy crawl quotes
```
Running spider without a project as a single file
```markdown
  scrapy runspider quotes
  scrapy runspider spiders/quotes_spider
```
### Scraping Wikipedia
#### Syntax
For the CSV delimiter, you can set in settings.py or when you execute the spider in CLI In settings.py

CSV_DELIMITER = "\t" # For tab

Untested Not working https://gist.github.com/oussama-ht/451b51dce2b208ae6276e955bb9e2528
```markdown
scrapy crawl spidername --set FEED_URI=output.csv --set FEED_FORMAT=csv --set CSV_DELIMITER=';'
scrapy crawl my_spider -o output.csv -t csv -a CSV_DELIMITER="|"
scrapy crawl wikipedia -o wikiurls.csv --set CSV_DELIMITER="|"
```

Wikipedia crawling commands
```markdown
scrapy crawl wikipedia
scrapy crawl wikipedia -o wikiurls.csv
```

```markdown

 scrapy parse --spider=wikipedia -d 3 https://en.wikipedia.org/wiki/google
 scrapy parse --spider=wikipedia -c parse_item -d 2 https://en.wikipedia.org/w/api.php?action=opensearch&format=json&formatversion=2&search=apple&namespace=0&limit=10
 ```


## BHGRE New Jersey Property Spider

This section documents the current BHGRE spider behavior for New Jersey statewide extraction.

### Current State (2026-05-19)

- Spider: `scrapy_crawlers/spiders/bhgre_spider.py` (`name = bhgre`).
- Crawl model: API-first with staged discovery and global dedupe.
- Strategy:
  1. ZIP shard discovery (`/api/neighborPlaces` with `placeType=postalCode`)
  2. City shard discovery (`/api/neighborPlaces` with `placeType=city`)
  3. State fallback shard (`/state/nj`)
- Each discovered canonical URL is resolved via `/api/places?brand=BHG&canonicalUrl=...`, then crawled through `POST /api/listings`.
- Every listing gets detail enrichment via `GET /api/listings/<id>?ctxCode=BHG&showMlsListings=true`.
- Deduplication is global by listing `id` across ZIP/city/state shards.

### Why State Counts Differed Before

- For NJ state, forcing `viewBoundary` undercounted results (~32K observed).
- Using state `placeMasterIds` without forced boundary matches the state page behavior (~42K observed).
- Current spider only sends `viewBoundary` when a shard explicitly provides it.

### New Strategy Flow

1. Warmup request (cookie/session priming): `https://www.bhgre.com/home/list/county/nj/bergen-county`
2. Seed ZIP shards from NJ state place ID.
3. Seed city shards from NJ state place ID.
4. Resolve each canonical (`/zip/nj/...`, `/city/nj/...`) to `placeMasterId`.
5. Crawl listing pages for each shard (`numPerPage=300`, `page=1..N`).
6. Run one state fallback shard (`/state/nj`) to catch anything missed.
7. Follow per-listing detail endpoint and merge base+detail fields.

### Options

Spider args:

| Arg | Description | Default |
|---|---|---|
| `max_pages` | Max listings pages per shard (`0` = no cap) | `0` |
| `place_num_per_page` | Page size for ZIP/city seed discovery | `200` |
| `max_place_pages` | Max seed pages per stage (`0` = no cap) | `0` |
| `enable_tiered_place_search` | Enable ZIP -> city -> state fallback strategy | `1` |

Environment overrides:

- `BHGRE_MAX_PAGES`
- `BHGRE_PLACE_NUM_PER_PAGE`
- `BHGRE_MAX_PLACE_PAGES`
- `BHGRE_ENABLE_TIERED_PLACE_SEARCH`
- `BHGRE_API_KEY`

### Commands

Smoke:

```bash
scrapy crawl bhgre -a max_pages=1 -a max_place_pages=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

Full no-cap run:

```bash
scrapy crawl bhgre -a max_pages=0 -a max_place_pages=0
```

Disable staged ZIP/city and run only state shard:

```bash
scrapy crawl bhgre -a enable_tiered_place_search=0
```

### HAR Verification

Useful HAR (example): `~/Downloads/www.bhgre.com5.har`

Key endpoints to verify:

- `GET /api/neighborPlaces/<NJ_PLACE_ID>?placeType=postalCode|city`
- `GET /api/places?brand=BHG&canonicalUrl=...`
- `POST /api/listings`
- `GET /api/listings/<listing_id>?ctxCode=BHG&showMlsListings=true`

Quick checks:

```bash
jq -r '.log.entries[] | .request.url | select(contains("www.bhgre.com/api/"))' ~/Downloads/www.bhgre.com5.har | sort -u

jq -r '.log.entries[] | select(.request.url=="https://www.bhgre.com/api/listings") | .request.postData.text' ~/Downloads/www.bhgre.com5.har | head -5
```

---

## GSMLS Spider

The GSMLS spider is implemented at `scrapy_crawlers/spiders/gsmls_spider.py` and follows the public multi-step flow:

1. County selection (`getcountysearch`)
2. Town selection (`getcommsearch`)
3. Criteria page (`getpropertysearch`)
4. Results page (`getpropertydetails`)

### Current State (2026-05-18)

- Result-cap handling: auto price-range splitting when GSMLS returns over-250 result warning.
- Detail enrichment: follows `moredetails.do` and merges detail fields onto card-level records.
- Field coverage expanded to include:
  - `property_remarks`, `style`
  - `full_baths`, `half_baths`
  - `garage_desc`, `heating`, `cooling`
  - tax fields and other labeled detail fields when present
- Style normalization now filters non-informative values such as `See Remarks`.
- Deterministic extraction with fallback parsing remains primary; optional LLM repair is still last-resort.

Smoke test:
```bash
scrapy crawl gsmls -a max_counties=1 -a max_towns=2 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

### Rendering Strategy — No Browser Required

GSMLS listing/search workflow is server-rendered HTML across multi-step form endpoints (`getcountysearch`, `getcommsearch`, `getpropertysearch`, `getpropertydetails`, `moredetails`).

- Primary extraction is deterministic selector parsing from server-rendered HTML.
- Fallback parsing uses scoped text/regex for drift resistance.
- Optional LLM repair is only for failed-card recovery, not primary rendering.

### Debugging (Spider-Relevant)

Key checks:

- result-cap split triggers on over-250 responses
- `moredetails` follow-up succeeds and merges fields
- drift ratio warnings align with true parser misses (not transient transport failures)
- normalized `style` excludes placeholder values like `See Remarks`

Quick smoke:

```bash
scrapy crawl gsmls -a max_counties=1 -a max_towns=2 -a disable_proxy=1 -s ITEM_PIPELINES={} -s TELNETCONSOLE_ENABLED=False
```

### How to Run

From the `scrapy_crawlers` directory:

```bash
scrapy crawl gsmls
```

Write to CSV:

```bash
scrapy crawl gsmls -O gsmls_output.csv
```

Optional scope limits for testing:

```bash
scrapy crawl gsmls -a max_counties=2 -a max_towns=5 -O gsmls_sample.csv
```

Run without configured proxy:

```bash
scrapy crawl gsmls -a disable_proxy=1 -O gsmls_output.csv
```

### Result Cap Handling

GSMLS returns an alert when a query exceeds 250 results.  
The spider detects this (`var count = '...'`) and automatically splits the query into min/max list-price ranges until each shard is within limit.  
MLS IDs are deduplicated globally across shards.

### Extraction Strategy

GSMLS responses used by the spider are server-rendered HTML. JavaScript is used for navigation helpers and UI behavior, but the listing data needed for scraping is present in the HTML response body.

The fix is not to stop using XPath. The fix is to stop writing fragile XPath.

Use this hierarchy:

1. Anchor to semantics first:
   - form field `name` / `id`
   - link patterns
   - stable text labels such as `MLS#`, `County`, `Beds`
   - hidden inputs
   - JavaScript function arguments when the page embeds structured data there
2. Avoid layout-dependent selectors:
   - bad: `//div[4]/div[2]/span[1]`
   - better: `//div[contains(., "MLS#")]`
   - better: select the listing card, then extract relative fields inside it
3. Add fallback extractors:
   - primary XPath/CSS
   - secondary regex on raw HTML
   - optional JSON/script parsing if data is embedded there
4. Validate fields:
   - `mls_id` matches the expected pattern
   - price is numeric
   - lat/lon is in range
   - required fields are present
5. Detect drift early:
   - log parse success rate
   - log missing required fields
   - save a raw snippet for failed cards

That gives you resilience without giving up deterministic extraction.

For these real estate sites, the preferred strategy is:

- XPath/CSS for primary extraction
- regex/script parsing for fallback
- monitoring for drift
- only use an LLM as a last-resort repair step for failed records

This is intentionally different from article-oriented tools such as Trafilatura or markdown extractors. For GSMLS, deterministic field extraction from HTML is the primary path because it is faster, easier to validate, and less error-prone for structured listing data.

### Where Implemented In Code

The GSMLS implementation lives in `scrapy_crawlers/spiders/gsmls_spider.py`.

- `parse_results`
  - handles the final results page for each county/town/price shard
  - checks whether GSMLS returned an over-limit result set and, if so, recursively splits the query by list price
  - finds listing-card nodes, parses each one, deduplicates by `mls_id`, yields valid listings, and logs per-shard metrics such as `display_count`, `page_seen`, and `parsed_new`
- `_parse_listing_card_primary`
  - performs the main deterministic extraction path
  - uses selector-based extraction anchored to listing-card-local structure, link patterns, hidden inputs, visible text, and embedded JS arguments
  - this is the first-pass parser and is intended to succeed for the normal page shape
- `_parse_listing_card_fallback`
  - runs when the primary parse is incomplete
  - extracts fields from raw HTML, regex matches, and embedded `openmapfromadd(...)` payloads
  - this is the backup path when markup shifts but structured values are still present in text or JavaScript
- `_validate_listing`
  - checks that extracted data is usable before yielding it
  - validates presence of core identifiers/location fields and normalizes numeric/state/ZIP/lat-lon values
  - clears obviously invalid values and records validation errors for drift reporting
- `_build_failed_card_artifact`
  - creates a structured drift artifact when parsing or validation fails
  - stores county/town context, failure reason, selector hints, a compact snippet, truncated raw HTML, and extracted text lines
  - these artifacts are used for debugging parser drift and reviewing likely future selector updates
- `_check_drift`
  - compares parsed-card coverage against the page's reported `display_count`
  - warns when coverage drops below `GSMLS_DRIFT_MIN_PARSE_RATIO`
  - can stop the spider early when `GSMLS_DRIFT_FAIL_FAST=1`
- `_selector_hints`
  - inspects a failed card and logs heuristic candidate selectors
  - provides likely XPath/CSS anchors for fields such as address, MLS label, status, and hidden lat/lon inputs
  - this is a maintenance aid, not an automatic selector rewrite
- `_attempt_llm_repair` and `_ollama_repair_listing`
  - optionally call a local Ollama model only for cards that deterministic parsing could not recover cleanly
  - build a constrained prompt from the failed card fragment, parse the returned JSON, and revalidate it before use
  - this path is disabled by default and is intended only as a last-resort repair step

Failed-card drift artifacts are written by the pipeline in `scrapy_crawlers/pipelines/pipelines.py`:

- local JSONL artifact sink: `DriftArtifactJsonLinesPipeline`

Drift reporting currently includes:

- per-shard `display_count`, `page_seen`, and `parsed_new` logging from `parse_results`
- validation-failure logging with selector hints and compact HTML snippets
- drift-threshold warnings or fail-fast shutdowns from `_check_drift`
- local JSONL persistence of failed-card artifacts through `DriftArtifactJsonLinesPipeline`

### Drift Monitoring And LLM Repair

The spider includes runtime drift monitoring for result pages:

- it warns when parse coverage falls below `GSMLS_DRIFT_MIN_PARSE_RATIO` (default `0.8`)
- it can stop the crawl on suspected drift when `GSMLS_DRIFT_FAIL_FAST=1`

Drift monitoring runs per shard. After page processing, `_check_drift` compares:

- `display_count`
- `page_seen`
- `parsed_new`

If parse coverage drops below threshold:

- it logs a warning
- or raises `CloseSpider` if fail-fast is enabled

Optional LLM repair is available for failed cards only. It is disabled by default.

- enable with `-a llm_repair=1` or `GSMLS_LLM_REPAIR_ENABLED=1`
- choose a local Ollama model with `GSMLS_LLM_REPAIR_MODEL` (default `llama3:8b`)
- set the Ollama base URL with `GSMLS_LLM_REPAIR_URL` (default `http://127.0.0.1:11434`)

This repair path is only called after deterministic parsing fails or validation fails. Repaired output is still validated before it is yielded.

`Ollama + Crawl4AI` can be used as a repair lane, but it should not replace the main Scrapy extraction path for GSMLS.

Recommended architecture:

1. Use Scrapy with deterministic XPath/CSS extraction as the primary path.
2. Save failed-card artifacts with selector hints, snippets, and raw HTML.
3. Use local Ollama repair against those artifacts first.
4. Only if the artifact is insufficient, use Crawl4AI to fetch a JS-rendered or cleaned page and pass that result into the repair step.
5. Revalidate any repaired fields before yielding or promoting them into the spider.

This keeps the normal crawl fast and deterministic while still giving you a recovery path for markup drift or client-rendered variants.

For GSMLS specifically, Crawl4AI is not part of the normal crawl because the current listing flow is primarily server-rendered HTML. It becomes useful only for unresolved failed records or future target sites that require browser rendering.

If this repair path grows to serve multiple spiders or needs queueing, scaling, or separate deployment, it can be promoted into a microservice later. The current implementation keeps repair local and artifact-driven because failed-card repair should be the exception, not the default crawl path.

When to keep repair local:

- only one or a small number of spiders need repair
- failed-card repair volume is low
- the repair input is already captured in local drift artifacts
- you want the simplest development and deployment model
- browser-rendered recrawls are occasional, not routine

When to promote repair into a microservice:

- multiple spiders or projects need the same repair capability
- you need a shared browser-rendering service
- repair jobs need queueing, retries, rate limiting, or scheduling
- you want separate scaling for repair workload
- you need a separate operational boundary, observability layer, or deployment lifecycle

Pros of a repair microservice:

- shared repair capability across crawlers
- central place for browser rendering, model access, and repair logic
- easier to add queueing, retries, metrics, and alerting
- independent scaling from the main Scrapy workers
- easier to evolve into an internal platform component

Cons of a repair microservice:

- more moving parts and more operational overhead
- more failure modes across network, service health, and request timeouts
- harder local development and debugging
- more latency than in-process repair
- risk of overusing expensive repair logic if the boundary is too easy to call

Additional design guidance from this discussion:

- do not refetch only by `url` and `missing_field` if you already have the failed-card artifact
- prefer artifact-driven repair because the original failed HTML fragment is the most reliable debugging input
- if a service is used, send the failed-card artifact JSON, not just the page URL
- the service should return candidate selectors, repaired fields, confidence, and notes
- do not automatically rewrite the spider with LLM-generated selectors
- keep selector promotion as a reviewed maintenance step
- use Crawl4AI only when JS-rendered or cleaned HTML is actually needed for repair
- use Ollama or another LLM only as a last-resort repair step after deterministic parsing and fallback extraction fail

For persistence, keep two layers:

- structured crawl output in MongoDB
- drift artifacts such as failed-card snippets and selector hints in object storage such as GCS

Object storage is the better default for drift artifacts because the payload is irregular, verbose, and mainly useful for debugging and offline analysis. Use a database only if you need to query and trend these artifacts operationally across runs.

For local development, the project now writes failed-card drift artifacts to JSON lines files through a Scrapy pipeline:

- default directory: `drift_artifacts/`
- GSMLS file example: `drift_artifacts/gsmls_failed_cards.jsonl`
- override directory with `DRIFT_ARTIFACTS_DIR`

These artifact records are written locally and dropped before MongoDB persistence or CSV feed export.

### Scale Guidance

The repair path should change as crawl volume increases. The core principle is that deterministic extraction must do the majority of the work, and expensive repair should stay rare.

#### Local and small-scale development

Use:

- deterministic Scrapy extraction
- local drift artifact JSONL files
- local Ollama repair only if needed
- no microservice required

This is the simplest model and is appropriate while tuning parsers or validating site-specific logic.

#### Around 200K URLs

At this scale, local files are still useful for debugging, but failure handling should begin moving toward structured events and selective escalation.

Recommended approach:

- keep deterministic parsing as the primary path
- emit structured failure artifacts, not just failed URLs
- use metrics to decide whether to escalate failures
- only route a subset of failures to repair workers
- separate Ollama-only repair from Crawl4AI JS-rendered repair

Do not use a sidecar proxy as the main control plane. If a sidecar is used, it should act only as a shipper for logs or artifacts, not as the main decision layer.

#### Around 10M URLs

At this scale, local-file-driven orchestration is no longer enough. Even small failure percentages become large operational volumes.

Recommended approach:

- use a durable queue or event stream for failure artifacts
- store raw artifacts in object storage
- cluster failures by template, missing-field pattern, or response signature
- route only a budgeted subset to repair
- fix parser drift at the template level and replay affected pages or shards

At 10M, the goal is not to repair every failed page individually. The goal is to identify recurring failure classes and fix them once.

`Template-level` remediation means fixing a reusable page structure or page type, not fixing one URL at a time. In practice this usually maps to page families such as:

- property detail pages
- search result listing cards
- agent profile pages
- office pages
- sold listing pages

Pages in the same template family usually share similar HTML structure, data blocks, hidden inputs, and JavaScript patterns. If many URLs fail with the same missing fields or selector break, the correct response is usually to repair the parser for that page structure and then replay the affected URLs, rather than running page-by-page repair for each URL.

If a specific page type breaks, such as property detail pages, the preferred handling is:

1. sample a small number of failed URLs from that page type
2. inspect the failed artifacts and, if needed, use Crawl4AI on the sample pages
3. identify the new DOM or rendering pattern for that page family
4. update the parser once for that page type
5. replay or recrawl the affected property-detail URLs

This is usually better than sending every failed property URL through expensive page-level repair.

Repair workflow:

1. deterministic parsing runs first
2. fallback regex/raw-HTML extraction runs second
3. validation checks required identifiers and field sanity
4. failed-card artifacts are created and persisted
5. optional Ollama repair is attempted only for failed records
6. repaired output is revalidated before it is used
7. repeated failures are clustered to decide whether a parser fix is needed

Pseudo-flow:

```text
results page
  -> primary parse
  -> fallback parse
  -> validate
      -> success: yield listing
      -> fail:
           build failed-card artifact
           write artifact JSONL
           if LLM repair enabled:
               send artifact context to Ollama
               validate repaired result
               if valid: yield repaired listing
  -> _check_drift compares display_count, page_seen, parsed_new
      -> below threshold: log warning
      -> fail-fast enabled: raise CloseSpider
```

Do not auto-update XPath rules directly from repair output.

Reasons:

- an LLM-generated selector may work for one failed page but be wrong for the broader template
- one failed page can produce a selector that breaks other templates
- temporary markup variants can be mistaken for stable structure
- automatic selector rewrites can silently corrupt extraction at scale

Safer approach:

1. detect drift
2. capture failed artifacts
3. generate candidate XPath/CSS selectors
4. cluster similar failures by template or signature
5. review the candidate selectors
6. update the parser manually
7. replay the affected pages after the change

What can be automated safely:

- artifact capture
- selector suggestion
- failure clustering
- confidence scoring
- test generation against stored samples
- replay scheduling after a human-approved parser change

If more automation is needed, the furthest recommended step is:

- auto-create a proposed patch or config diff
- run it against a validation corpus
- require approval before merge or deploy

Selector suggestion can be automated. Selector promotion into the production spider should remain a reviewed maintenance step.

Summary:

- auto-suggest selectors: yes
- auto-commit or auto-apply selector changes to the production spider: no

For high-volume crawls, template-level review is much cheaper than cleaning up silent parser damage.

#### Around 80M URLs

At this scale, the system is a distributed data pipeline, not just a crawler with a repair helper.

Required characteristics:

- centralized eventing for failure artifacts
- object storage for failed HTML snippets and rendered snapshots
- failure classification and clustering
- repair budgets and rate limits
- distributed repair workers
- template-level parser remediation and replay pipelines

Do not use:

- local files as the primary trigger
- a sidecar proxy as the orchestration layer
- page-by-page LLM or browser-render repair for all failures

At 80M, page-level repair must remain exceptional. The main leverage comes from classifying failures, identifying template drift, patching the parser, and replaying the affected population.

#### Sidecar versus dispatcher

A sidecar can be acceptable for:

- log shipping
- artifact shipping
- local buffering

A sidecar is not the right primary control plane for:

- failure classification
- escalation policy
- repair routing
- fleet-wide deduplication

For medium and large-scale crawls, use a dispatcher or classifier service with a queue and object storage instead of a file-watching sidecar proxy.

#### Technology choices by concern

For medium and large-scale crawls, separate the system by concern instead of trying to make the crawler process do everything.

Control plane:

- `Kafka`
  - strong choice for high-throughput event streaming and replay
  - good fit when many crawler workers and repair workers are active at once
- `Google Pub/Sub`
  - good managed option if the platform is already on GCP
  - simpler operationally than Kafka
- `Amazon SQS` plus workers
  - simpler queueing model if full event streaming is not needed
  - good fit for task dispatch and retry workflows
- `RabbitMQ`
  - workable for moderate throughput and routing patterns
  - less attractive than Kafka/PubSub for very large replay-heavy pipelines

Artifact storage:

- `Google Cloud Storage (GCS)`
  - good default if running on GCP
  - suitable for failed HTML fragments, rendered snapshots, JSONL artifacts, and replay inputs
- `Amazon S3`
  - same role as GCS in AWS environments
- local filesystem
  - acceptable only for development or small runs
  - not suitable as the system of record at large scale

Metrics and observability:

- `Prometheus` + `Grafana`
  - good default for operational metrics, alerting, and dashboards
  - use for parse success rate, drift events, repair volume, retry counts, and queue depth
- `OpenTelemetry`
  - useful if traces and standardized telemetry are needed across crawler, dispatcher, and repair workers
- `ClickHouse`, `BigQuery`, or `Elasticsearch`
  - useful for large-scale analytical queries on crawl logs, failure signatures, and historical drift trends

State and dedupe:

- `state` means operational memory for the crawl and repair pipeline:
  - which URLs already failed
  - which artifacts already went to repair
  - which failure clusters are known
  - which replay jobs are already running
  - which domains or templates have exhausted their repair budgets
- `dedupe` means preventing repeated work:
  - do not queue the same failed artifact many times
  - do not rerender the same page repeatedly
  - do not repair the same cluster over and over
  - do not insert the same listing or create duplicate repair jobs

- `Redis`
  - good for fast transient state such as retry budgets, recent failure signatures, queue coordination, and short-lived dedupe
- relational database (`PostgreSQL`, `Cloud SQL`, `AlloyDB`, `RDS`)
  - good for durable repair state, review queues, parser version tracking, and replay bookkeeping
- analytical warehouse (`BigQuery`, `Snowflake`, `ClickHouse`)
  - good for large-scale historical analysis, clustering results, and long-term trend reporting

Typical dedupe keys include:

- `mls_id` for listings
- `url` for page-level retries
- artifact hash for failed-card snippets or raw HTML fragments
- failure signature hash for repeated parser drift
- `(artifact_id, repair_strategy)` for repair jobs
- `(site, url, parser_version)` for replay control

Recommended split:

- control plane: `Kafka` or managed `Pub/Sub`
- artifact storage: `GCS` or `S3`
- metrics: `Prometheus` + `Grafana`
- state/dedupe: `Redis` for fast ephemeral control, plus a durable database for repair/replay state

The key design point is to keep these roles separate:

- the queue carries failure and repair events
- object storage keeps debug and repair payloads, not the normal extracted listing data. Examples include:
  - raw HTML fragments
  - rendered HTML snapshots
  - full page DOM dumps
  - failed-card snippets
  - text lines from failed cards
  - selector hints
  - screenshots if browser capture is added later
  - repair prompts and repair responses for audit/debug
- metrics systems track system health and drift rates
- state stores enforce dedupe, retry budgets, and replay coordination

#### Repair budgets

At medium and large scale, repair capacity must be treated as a budgeted resource. Browser rendering and LLM-based repair are too expensive to leave unconstrained.

What to budget:

- maximum failed artifacts escalated per spider per run
- maximum JS-render repairs per domain per hour
- maximum LLM repairs per template per day
- maximum retries per failure class
- maximum sampled artifacts per repeated failure cluster
- maximum total repair spend or repair runtime window per batch

Why budgets are necessary:

- failure spikes should not overwhelm repair workers
- repeated template failures should be clustered and fixed once, not repaired page by page
- JS rendering can become the dominant cost if it is used too broadly
- LLM repair should remain the exception path, not the normal extraction path

Recommended policy:

1. deterministic parsing and local fallback handle the default path
2. only a bounded subset of failures is escalated
3. repeated failures are clustered before expensive repair is attempted
4. template-level parser fixes are preferred over large-scale page-level repair
5. replay affected pages after parser fixes instead of repeatedly paying repair cost

Practical examples:

- cap Crawl4AI rendering to a fixed number of pages per domain per hour
- cap Ollama repair attempts to a fixed percentage of failed artifacts
- stop escalating a known repeated failure cluster once enough samples have been collected
- require a higher priority threshold before sending low-value pages to repair

Budgets should be enforced in the dispatcher or classifier layer, not in the sidecar or only inside the spider. The crawler should emit artifacts and metrics; the control plane should decide what portion of failures is worth escalating.

### Environment Settings

The spider reads environment variables from `scrapy_crawlers/settings/.env` through `spiders/env_config.py`.

Required for MongoDB pipeline:

- `MONGO_URI`
- `MONGO_DATABASE`

Optional MongoDB settings:

- `DB_MAX_POOL_SIZE`
- `DB_MIN_POOL_SIZE`
- `DB_COMPRESSORS`
- `DB_ZLIB_COMPRESSION_LEVEL`
- `MONGO_COLLECTION_PREFIX`

Optional proxy settings:

- `PROXY_HOST`
- `PROXY_PORT`
- `LOGIN`
- `PASSWORD`
- optional `PROXY_PARAMS` or `DATAIMPULSE_PROXY_PARAMS`

If proxy settings are missing, the GSMLS spider can still run directly with:

```bash
scrapy crawl gsmls -a disable_proxy=1
```

Optional GSMLS drift settings:

- `GSMLS_DRIFT_MIN_PARSE_RATIO=0.8`
- `GSMLS_DRIFT_FAIL_FAST=0`
- `DRIFT_ARTIFACTS_DIR=drift_artifacts`

Optional GSMLS LLM repair settings:

- `GSMLS_LLM_REPAIR_ENABLED=0`
- `GSMLS_LLM_REPAIR_MODEL=llama3:8b`
- `GSMLS_LLM_REPAIR_URL=http://127.0.0.1:11434`
- `GSMLS_LLM_REPAIR_TIMEOUT=20`

LLM repair is off by default. It is only used for failed cards after deterministic parsing or validation fails.

To use the Ollama repair path, you need a local Ollama server running and the model pulled locally. Typical setup:

```bash
ollama serve
ollama pull llama3:8b
```

If Ollama is running on a different host or port, set `GSMLS_LLM_REPAIR_URL` accordingly.

Ollama setup steps:

1. Install Ollama on the machine running the spider.
2. Start the local Ollama server:

```bash
ollama serve
```

3. Pull the model used by the spider:

```bash
ollama pull llama3:8b
```

4. Verify the server is listening on the default local endpoint:

```bash
curl http://127.0.0.1:11434/api/tags
```

5. Enable repair in `.env` or shell:

```dotenv
GSMLS_LLM_REPAIR_ENABLED=1
GSMLS_LLM_REPAIR_MODEL=llama3:8b
GSMLS_LLM_REPAIR_URL=http://127.0.0.1:11434
GSMLS_LLM_REPAIR_TIMEOUT=20
```

6. Run the spider:

```bash
cd scrapy_crawlers
scrapy crawl gsmls -a disable_proxy=1
```

If you want the repair path off, leave `GSMLS_LLM_REPAIR_ENABLED=0`. The spider will continue using deterministic parsing, fallback extraction, and drift reporting without Ollama.

Example `.env` block:

```dotenv
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>/?appName=RealEstateCrawler
MONGO_DATABASE=realestatecrawler
DB_MAX_POOL_SIZE=10

GSMLS_DRIFT_MIN_PARSE_RATIO=0.8
GSMLS_DRIFT_FAIL_FAST=0
DRIFT_ARTIFACTS_DIR=drift_artifacts

GSMLS_LLM_REPAIR_ENABLED=0
GSMLS_LLM_REPAIR_MODEL=llama3:8b
GSMLS_LLM_REPAIR_URL=http://127.0.0.1:11434
GSMLS_LLM_REPAIR_TIMEOUT=20
```

### Output Fields

Each item includes fields such as:

- `mls_id`, `sys_id`
- `address`, `city`, `state`, `postal_code`
- `county`, `county_code`, `town`, `town_code`
- `status`, `list_price`, `style`
- `rooms`, `beds`, `baths`, `acres`
- `latitude`, `longitude`
- `search_min_list_price`, `search_max_list_price`
