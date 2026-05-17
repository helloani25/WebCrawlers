## NJMLS Spider

The NJMLS spider extracts active residential listings from the New Jersey Multiple Listing Service public search portal, crawling all 21 NJ counties.

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

---

## RE/MAX Spider

The RE/MAX spider extracts for-sale listings from `www.remax.com` using the same Next.js RSC (`_rsc`) listing endpoint pattern captured in the HAR.

### How to Run

From `scrapy_crawlers`:

```bash
../.venv/bin/scrapy crawl remax -a state=nj -a max_pages=50 -a disable_proxy=1 -o remax_output.csv
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

---

## Redfin Spider
The Redfin spider extracts for-sale listings across all 21 New Jersey counties using Redfin's Stingray GIS API.

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
../.venv/bin/scrapy crawl redfin -a disable_proxy=1 -a max_counties=1 -a max_pages_per_county=2 -o redfin_test.csv
```

### Spider Flow
1. Warm up on `https://www.redfin.com/` for session/cookies.
2. Iterate NJ counties using static county -> `region_id` mapping.
3. Call:
   - `GET https://www.redfin.com/stingray/api/gis`
   - params: `region_type=5`, `rets=LIST_COUNT`, `num_homes=350`, `page_number=N`, filters
4. Parse `payload.homes` and normalize listing fields.
5. Continue paging while homes are returned (bounded by `max_pages_per_county`).
6. Deduplicate globally by `listing_id` (fallback `mls_id`, then `detail_url`).

### Property Detail Extraction
For each Redfin listing URL, the spider extracts/normalizes:
- Core identity: `listing_id`, `mls_id`, `detail_url`, `county`, `region_id`
- Address: `address`, `city`, `state`, `postal_code`
- Pricing/status/type: `list_price`, `status`, `property_type`
- Size/layout: `beds`, `baths`, `build_area_sqft`, `lot_size_sqft`, `lot_size_acres`, `stories`
- Structural/location: `year_built`, `latitude`, `longitude`
- Additional: `description`

Primary source is the GIS payload (`payload.homes[]`) with fallback enrichment from AVM when address fields are missing.

### Address and Detail Fallbacks
- Primary address source: GIS payload fields (`address.*`, `homeData.addressInfo.*`).
- Fallback 1: derive `address/city/state/postal_code` from Redfin detail URL slug  
  (example `/NJ/City/Street-Name-07652/home/123`).
- Fallback 2: when GIS item still has missing address fields, request:
  - `GET /stingray/api/home/details/avm?propertyId=...&listingId=...&accessLevel=1&pageType=1`
  and merge address fields from AVM payload.
- Fallback 3: if GIS is blocked/fails (`401/403/405/429` or non-200), fetch county HTML page and extract `/home/<id>` URLs from inline JS so listing URLs are still captured.

### HAR Notes (www.redfin.com.har)
Use HAR captures to validate current Redfin endpoint usage:

```bash
jq -r '.log.entries[] | .request.url | select(contains("redfin.com/stingray"))' \
  ~/Downloads/www.redfin.com.har | sort -u
```

The key listing endpoint for this spider is:
- `https://www.redfin.com/stingray/api/gis`

Address-enrichment endpoint:
- `https://www.redfin.com/stingray/api/home/details/avm`

If a HAR capture is from a single listing-detail browsing session, it may contain mostly tracking + detail calls and very few `api/gis` records. In that case, capture a county search browsing session.

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
1. Warm up on `https://www.zillow.com/nj/` to establish cookies/session state.
2. Submit `PUT /async-create-search-page-state` with NJ `regionSelection`, NJ `regionBounds`, and `pagination.currentPage`.
3. If a query hits Zillow's cap (`totalPages >= 20` with very high totals), split `mapBounds` into 4 quadrants and recurse.
4. Parse each listing from `cat1.searchResults.listResults`, dedupe globally by `zpid`, and continue paginating each shard until `currentPage == totalPages`.

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

This section documents the development process for the BHGRE spider that extracts all property listings from Better Homes and Gardens Real Estate (BHGRE) for the state of New Jersey.

### Development Process

#### 0. Preparation for the Spider - Processing the HAR File
Before writing the spider, we analyzed a HAR file captured from the BHGRE website to understand

1. Let me parse the HAR file to understand the API endpoints:
 ```
bash  python3 << 'EOF'
import json

with open('~/Downloads/www.bhgre.com.har', 'r') as f:
    har = json.load(f)

# Extract all unique URLs from the HAR file
urls = set()
for entry in har.get('log', {}).get('entries', []):
    request = entry.get('request', {})
    url = request.get('url', '')
    if url:
        urls.add(url)

# Print URLs that look like API calls
for url in sorted(urls):
    if 'api' in url.lower() or 'search' in url.lower():
        print(url)
EOF

```
2. Now let me extract the request and response details for the listings API:
```bash
python3 << 'EOF'
import json

with open('~/Downloads/www.bhgre.com.har', 'r') as f:
    har = json.load(f)

# Find listings API calls
for entry in har.get('log', {}).get('entries', []):
    request = entry.get('request', {})
    url = request.get('url', '')
    
    if 'api/listings' in url and url.endswith('api/listings'):
        print("=" * 80)
        print("URL:", url)
        print("METHOD:", request.get('method'))
        print("\nRequest Headers:")
        for header in request.get('headers', []):
            print(f"  {header.get('name')}: {header.get('value')}")
        
        if request.get('postData'):
            print("\nRequest Body:")
            body = request.get('postData', {}).get('text', '')
            try:
                body_json = json.loads(body)
                print(json.dumps(body_json, indent=2)[:1000])
            except:
                print(body[:1000])
        
        response = entry.get('response', {})
        print("\nResponse Status:", response.get('status'))
        
        if response.get('content'):
            content = response.get('content', {}).get('text', '')
            try:
                content_json = json.loads(content)
                print("\nResponse Body (first part):")
                print(json.dumps(content_json, indent=2)[:2000])
            except:
                print("\nResponse Body:")
                print(content[:1000])
        print()
EOF
```
3. Let me try a simpler approach to extract the API details:
```bash
python3 << 'EOF'
import json

with open('~/Downloads/www.bhgre.com.har', 'r') as f:
    har = json.load(f)

# Find listings API calls
for entry in har.get('log', {}).get('entries', []):
    request = entry.get('request', {})
    url = request.get('url', '')
    
    if 'api/listings' in url and url.endswith('api/listings'):
        print("URL:", url)
        print("METHOD:", request.get('method'))
        
        if request.get('postData'):
            body = request.get('postData', {}).get('text', '')
            try:
                body_json = json.loads(body)
                print("\nRequest Body:")
                print(json.dumps(body_json, indent=2))
            except:
                print("\nRequest Body (raw):", body[:500])
        
        response = entry.get('response', {})
        print("\nResponse Status:", response.get('status'))
        print("\nResponse Content-Type:", response.get('content', {}).get('mimeType'))
        
        if response.get('content'):
            content = response.get('content', {}).get('text', '')
            try:
                content_json = json.loads(content)
                # Get a sample property
                if 'listings' in content_json:
                    print("\n# Sample listing:")
                    print(json.dumps(content_json['listings'][0] if content_json['listings'] else {}, indent=2))
                else:
                    print("\nResponse keys:", list(content_json.keys()))
                    print(json.dumps(content_json, indent=2)[:1500])
            except Exception as e:
                print(f"\nError parsing: {e}")
        break
EOF
```
4. Let me save the extraction to a file to avoid terminal issues:
```bash
python3 > /tmp/har_analysis.txt 2>&1 << 'EOF'
import json

with open('~/Downloads/www.bhgre.com.har', 'r') as f:
    har = json.load(f)

# Find listings API calls
for entry in har.get('log', {}).get('entries', []):
    request = entry.get('request', {})
    url = request.get('url', '')
    
    if 'api/listings' in url and url.endswith('api/listings'):
        print("URL:", url)
        print("METHOD:", request.get('method'))
        
        if request.get('postData'):
            body = request.get('postData', {}).get('text', '')
            try:
                body_json = json.loads(body)
                print("\nRequest Body:")
                print(json.dumps(body_json, indent=2)[:2000])
            except:
                print("\nRequest Body (raw):", body[:500])
        
        response = entry.get('response', {})
        print("\nResponse Status:", response.get('status'))
        
        if response.get('content'):
            content = response.get('content', {}).get('text', '')
            try:
                content_json = json.loads(content)
                if 'listings' in content_json:
                    print("\nNumber of listings:", len(content_json['listings']))
                    if content_json['listings']:
                        print("\nSample listing:")
                        print(json.dumps(content_json['listings'][0], indent=2)[:3000])
                else:
                    print("\nResponse keys:", list(content_json.keys())[:10])
            except Exception as e:
                print(f"Error: {e}")
        break
EOF
cat /tmp/har_analysis.txt
```
```commandline
 python3 << 'EOF'
heredoc> import json
heredoc> with open('~/Downloads/www.bhgre.com.har', 'r') as f:
heredoc>     har = json.load(f)
heredoc> # Extract all unique URLs from the HAR file
heredoc> urls = set()
heredoc> for entry in har.get('log', {}).get('entries', []):
heredoc>     request = entry.get('request', {})
heredoc>     url = request.get('url', '')
heredoc>     if url:
heredoc>         urls.add(url)
heredoc> # Print URLs that look like API calls
heredoc> for url in sorted(urls):
heredoc>     if 'api' in url.lower() or 'search' in url.lower():
heredoc>         print(url)
heredoc> EOF
https://maps.googleapis.com/$rpc/google.internal.maps.mapsjs.v1.MapsJsInternalService/GetViewportInfo
https://maps.googleapis.com/maps/api/mapsjs/mapConfigs:batchGet?language=en-US&region=US&alt=protojson&major_version=63&minor_version=14d&map_ids=601103d70391a6d5&map_type=1
https://www.bhgre.com/api/ai/content?brandCode=BHG&placeMasterId=P02500000GAeYCEazA8yNXYKU829IUlj7wpEXLY1&page_type=listing&content_type=faq
https://www.bhgre.com/api/listings
https://www.bhgre.com/api/neighborPlaces/P02500000GAeYCEazA8yNXYKU829IUlj7wpEXLY1?brand=BHG&placeType=city&applyListingsFilter=true&&&page=1&numPerPage=20
https://www.bhgre.com/api/neighborPlaces/P02500000GAeYCEazA8yNXYKU829IUlj7wpEXLY1?brand=BHG&placeType=neighborhood&applyListingsFilter=true&&&page=1&numPerPage=20
https://www.bhgre.com/api/neighborPlaces/P02500000GAeYCEazA8yNXYKU829IUlj7wpEXLY1?brand=BHG&placeType=postalCode&applyListingsFilter=true&&&page=1&numPerPage=20
https://www.bhgre.com/api/places/search
https://www.bhgre.com/api/places?brand=BHG&canonicalUrl=%2Fcounty%2Fnj%2Fbergen-county
https://www.bhgre.com/api/searchsuggestions
https://www.google-analytics.com/g/collect?v=2&tid=G-B8L3MV0KHE&gtm=45je64s1v9112769574z8889086970za20gzb889086970zd889086970&_p=1777532412716&gcs=G100&gcd=13q3q3q3q5l1&npa=1&dma_cps=-&dma=0&gdid=dNTIxZG&_eu=AAAAAGA&are=1&cid=2086974229.1777532413&frm=0&lps=1&pscdl=denied&rcb=0&sr=1728x1117&uaa=arm&uab=64&uafvl=Google%2520Chrome%3B147.0.7727.117%7CNot.A%252FBrand%3B8.0.0.0%7CChromium%3B147.0.7727.117&uam=&uamb=0&uap=macOS&uapv=15.7.3&uaw=0&ul=en-us&gaf=2&_s=10&tag_exp=0~115938466~115938469~117266400~117512543~118463261&dp=%2Fhome%2Flist%2Fcounty%2Fnj%2Fbergen-county&dl=https%3A%2F%2Fwww.bhgre.com%2Fhome%2Flist%2Fcounty%2Fnj%2Fbergen-county&dr=https%3A%2F%2Fwww.google.com%2F&dt=Bergen%20County%2C%20NJ%20Homes%20for%20Sale%20with%20Style%20%7C%20BHGRE&sid=1777532412&sct=1&seg=1&_tu=CA&en=page_view&ep.content_group=Property%20Search%20Results&_et=12009&tfd=142759
https://www.google-analytics.com/g/collect?v=2&tid=G-B8L3MV0KHE&gtm=45je64s1v9112769574za20gzb889086970zd889086970&_p=1777532412716&gcs=G100&gcd=13q3q3q3q5l1&npa=1&dma_cps=-&dma=0&gdid=dNTIxZG&_eu=AEEAAGA&ae=a&are=1&cid=2086974229.1777532413&frm=0&lps=1&pscdl=denied&rcb=0&sr=1728x1117&uaa=arm&uab=64&uafvl=Google%2520Chrome%3B147.0.7727.117%7CNot.A%252FBrand%3B8.0.0.0%7CChromium%3B147.0.7727.117&uam=&uamb=0&uap=macOS&uapv=15.7.3&uaw=0&ul=en-us&gaf=2&_s=8&tag_exp=0~115938466~115938469~117266400~117512543~118463261&dp=%2F&dl=https%3A%2F%2Fwww.bhgre.com%2F&dr=https%3A%2F%2Fwww.google.com%2F&sid=1777532412&sct=1&seg=1&dt=Buy%20a%20Home%20That%20Fits%20Your%20Lifestyle%20%7C%20BHGRE&_tu=CA&en=form_start&ep.content_group=Ungrouped&ep.form_id=&ep.form_name=&ep.form_destination=https%3A%2F%2Fwww.bhgre.com%2Fhome%2Fbuy&epn.form_length=9&ep.first_field_id=locationSearch_id&ep.first_field_name=locationSearch&ep.first_field_type=text&epn.first_field_position=1&_et=45977&tfd=125734
(.venv) WebCrawlers % python3 << 'EOF'
heredoc> import json
heredoc> with open('~/Downloads/www.bhgre.com.har', 'r') as f:
heredoc>     har = json.load(f)
heredoc> # Find listings API calls
heredoc> for entry in har.get('log', {}).get('entries', []):
heredoc>     request = entry.get('request', {})
heredoc>     url = request.get('url', '')
heredoc>     if 'api/listings' in url and url.endswith('api/listings'):
heredoc>         print("=" * 80)
heredoc>         print("URL:", url)
heredoc>         print("METHOD:", request.get('method'))
heredoc>         print("\nRequest Headers:")
heredoc>         for header in request.get('headers', []):
heredoc>             print(f"  {header.get('name')}: {header.get('value')}")
heredoc>         if request.get('postData'):
heredoc>             print("\nRequest Body:")
heredoc>             body = request.get('postData', {}).get('text', '')
heredoc>             try:
heredoc>                 body_json = json.loads(body)
heredoc>                 print(json.dumps(body_json, indent=2)[:1000])
heredoc>             except:
heredoc>                 print(body[:1000])
heredoc>         response = entry.get('response', {})
heredoc>         print("\nResponse Status:", response.get('status'))
heredoc>         if response.get('content'):
heredoc> with open(      har = json.load(f)
heredoc> # Find listings API calls
heredoc> for entry in har t# Find listings API confor entry in har.get('loon    request = entry.get('request', {})
heredoc>     url = rt     url = request.get('url', '')
heredoc>     um    if 'api/listings' in url an0]        print("=" * 80)
heredoc>         print("URL:", url)
heredoc>         pr")        print("URL:", (c        print("METHOD:", in        python3 << 'EOF'
heredoc> import json
heredoc> with open('~/Downloads/www.bhgre.com.har', 'r') as f:
heredoc>     har = json.load(f)
heredoc> # Find listings API calls
heredoc> for entry in har.get('log', {}).get('entries', []):
heredoc>     request = entry.get('request', {})
heredoc>     url = request.get('url', '')
heredoc>     if 'api/listings' in url and url.endswith('api/listings'):
heredoc>         print("URL:", url)
heredoc>         print("METHOD:", request.get('method'))
heredoc>         if request.get('postData'):
heredoc>             body = request.get('postData', {}).get('text', '')
heredoc>             try:
heredoc>                 body_json = json.loads(body)
heredoc>                 print("\nRequest Body:")
heredoc>                 print(json.dumps(body_json, indent=2))
heredoc>             except:
heredoc>                 print("\nRequest Body (raw):", body[:500])
heredoc>         response = entry.get('response', {})
heredoc>         print("\nResponse Status:", response.get('status'))
heredoc>         print("\nResponse Content-Type:", response.get('content', {}).get('mimeType'))
heredoc>         if response.get('content'):
heredoc>             content = response.get('content', {}).get('text', '')
heredoc>     with open(y:    har = json.load(f)
heredoc> # Find listings API calls
heredoc> t)
heredoc>                # Find listings API crtfor entry in har.get('lostings' in content_json:
heredoc>                     print("\n    url = request.get('url', '')
heredoc>           if 'api/listings' in url an'l        print("URL:", url)
heredoc>         print("METHOD:", request.g          print("METHOD:",           if request.get('postData'):
heredoc>           nt            body = request.get('po              try:
heredoc>                 body_json = json.loads(body)                 n                 print("\nRequest Body:")
heredoc>   ng                print(jsonpython3 > /tmp/har_analysis.txt 2>&1 << 'EOF'
heredoc> import json
heredoc> with open('~/www.bhgre.com.har', 'r') as f:
heredoc>     har = json.load(f)
heredoc> # Find listings API calls
heredoc> for entry in har.get('log', {}).get('entries', []):
heredoc>     request = entry.get('request', {})
heredoc>     url = request.get('url', '')
heredoc>     if 'api/listings' in url and url.endswith('api/listings'):
heredoc>         print("URL:", url)
heredoc>         print("METHOD:", request.get('method'))
heredoc>         if request.get('postData'):
heredoc>             body = request.get('postData', {}).get('text', '')
heredoc>             try:
heredoc>                 body_json = json.loads(body)
heredoc>                 print("\nRequest Body:")
heredoc>                 print(json.dumps(body_json, indent=2)[:2000])
heredoc>             except:
heredoc>                 print("\nRequest Body (raw):", body[:500])
heredoc>         response = entry.get('response', {})
heredoc>         print("\nResponse Status:", response.get('status'))
heredoc>         if response.get('content'):
heredoc>             content = response.get('content', {}).get('text', '')
heredoc>             try:
heredoc>                 conteimport j= json.loads(content)
heredoc>                 with open(gs    har = json.load(f)
heredoc> # Find listings API calls
heredoc> for entry in hars:# Find listings API clifor gs']))
heredoc>                     if content_json['listings']:
heredoc>                       url = request.get('url', '')
heredoc>           if 'api/listings' in url anmp        print("URL:", url)
heredoc>         print("METHOD:", request.g          print("METHOD:",           if request.get('postData'):
heredoc>           ey            body = request.get('popt            try:
heredoc>                 body_json = json.loads(body)EO               an           cd /tmp && python3 parse_har.py 2>&1 | head -100
heredoc> 

```

#### 1. Analyzing the HAR File

The spider was built by analyzing a HAR (HTTP Archive) file captured from the BHGRE website. The HAR file contains network requests made during browsing.

**HAR File Location:** `~/Downloads/www.bhgre.com.har`

#### 2. Extracting API Endpoints with jq

First, we identified all API endpoints in the HAR file:

```bash
# Find all API-related URLs
jq '.log.entries[] | select(.request.url | contains("api")) | .request.url' ~/Downloads/www.bhgre.com.har | sort | uniq


(.venv)  % jq '.log.entries[] | select(.request.url | contains("api/listings")) | {url: .request.url, method: .request.method, status: .response.status}' ~/Downloads/www.bhgre.com.har
{
  "url": "https://www.bhgre.com/api/listings",
  "method": "POST",
  "status": 200
}
(.venv)  % jq '.log.entries[] | select(.request.url | contains("api/listings")) | .request.postData.text | fromjson' ~/Downloads/www.bhgre.com.har 2>&1
{
  "ctx": {
    "brandCode": "BHG",
    "language": "en-US"
  },
  "numPerPage": 300,
  "status": "ACTIVE,PENDING,COMING_SOON",
  "showMlsListings": true,
  "minNumImages": 0,
  "projectedFields": "projectedFields.UniversalPlatform",
  "placeMasterIds": "P02500000GAeYCEazA8yNXYKU829IUlj7wpEXLY1",
  "viewBoundary": {
    "topRightMapPoint": [
      -74.272226,
      40.76159
    ],
    "bottomLeftMapPoint": [
      -73.893628,
      41.133714
    ]
  },
  "propertyType": "SFR,MFR,MFD,CONDO,TOWNHOUSE,COOP,LAND,FARM",
  "sortBy": "[{\"key\":\"newListingTimeStamp\",\"order\":\"DESC\"}]"
}
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url | contains("api/listings")) | .response.content.text | fromjson | keys' ~/Downloads/www.bhgre.com.har
[
  "apiVersion",
  "data"
]
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url | contains("api/listings")) | .response.content.text | fromjson | .data | keys' ~/Downloads/www.bhgre.com.har
[
  "additionalInfo",
  "pagination",
  "results"
]
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url | contains("api/listings")) | .response.content.text | fromjson | .data.results[0]' ~/Downloads/www.bhgre.com.har | head -100
{
  "id": "P00800000HABAHtaBEZliPMeNEAE0iJCuzqXEFcQ",
  "idxFeedAttributionCompany": "Better Homes and Gardens Real Estate Maturo",
  "canonicalURL": "/nj/mahwah/30-n-bayard-ln/lid-P00800000HABAHtaBEZliPMeNEAE0iJCuzqXEFcQ",
  "isInBrand": false,
  "isLuxuryListing": false,
  "brand": null,
  "area": {
    "listingArea": null,
    "listingAreaUnits": "sq. ft.",
    "lotSize": 0.09,
    "lotSizeUnits": "Acres"
  },
  "attribution": {
    "listingOfficeName": "TERRIE O'CONNOR REALTORS",
    "agentName": "JOSEPH O CONNOR"
  },
  "chips": {
    "mediaChips": [],
    "statusChips": [
      {
        "label": "Open Sat, 12 to 3pm",
        "ariaLabel": "Open house Saturday, 12 to 3pm",
        "type": "TEXT"
      },
      {
        "label": "New",
        "type": "TEXT"
      }
    ]
  },
  "gis": {
    "latitude": 41.08041,
    "longitude": -74.134213
  },
  "location": {
    "unparsedAddress": "30 N Bayard Ln",
    "city": "Mahwah Twp.",
    "stateCode": "NJ",
    "postalCode": "07430"
  },
  "mls": {
    "mlsNumber": "4024260",
    "globalDisclaimer": {
      "logoUrl": null,
      "logoPosition": "beforeListings",
      "textPosition": "beforeListings",
      "text": "The data relating to real estate for sale on this website comes in part from the IDX Program of Garden State Multiple Listing Service, L.L.C. Real estate listings held by other brokerage firms are marked as IDX Listing. Information deemed reliable but not guaranteed. 2026 Garden State Multiple Listing Service, L.L.C. All rights reserved. Notice: The dissemination of listings on this website does not constitute the consent required by N.J.A.C. 11:5.6.1 (n) for the advertisement of listings exclusively for sale by another broker. Any such consent must be obtained in writing from the listing broker. This information is being provided for Consumers' personal, non-commercial use and may not be used for any purpose other than to identify prospective properties Consumers may be interested in purchasing. Date Last Updated April 30, 2026"
    }
  },
  "openHouses": [
    {
      "startTime": "2026-05-02T12:00:00.000Z",
      "endTime": "2026-05-02T15:00:00.000Z"
    },
    {
      "startTime": "2026-05-03T12:00:00.000Z",
      "endTime": "2026-05-03T15:00:00.000Z"
    }
  ],
  "photos": {
    "firstPhotoUrl": null,
    "media": [],
    "photosCount": 0
  },
  "price": 799999,
  "property": {
    "propertyType": "TOWNHOUSE",
    "listingStatus": "ACTIVE",
    "bedrooms": 2,
    "bathrooms": 4
  },
  "rules": {
    "hideMapPin": false,
    "propCardDisplayFullBrokerageName": null,
    "propCardDisplayLogo": true,
    "propCardMapViewDisplayLogo": null,
    "globalDisplayDisclaimerAndLogoFooter": null,
    "displayLastListPrice": false
  }
}
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url | contains("api/places")) | {url: .request.url, method: .request.method, status: .response.status}' ~/Downloads/www.bhgre.com.har
{
  "url": "https://www.bhgre.com/api/places/search",
  "method": "POST",
  "status": 200
}
{
  "url": "https://www.bhgre.com/api/places?brand=BHG&canonicalUrl=%2Fcounty%2Fnj%2Fbergen-county",
  "method": "GET",
  "status": 200
}
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url == "https://www.bhgre.com/api/places/search") | .request.postData.text | fromjson' ~/Downloads/www.bhgre.com.har
{
  "brand": "BHG",
  "placeType": "state",
  "pageSortBy": {
    "key": "placeName",
    "order": "asc"
  },
  "sortBy": {
    "key": "numberOfListings",
    "order": "desc"
  },
  "numPerPage": 100,
  "page": 1,
  "projectedFields": "placeMasterId,placeName,displayName,canonicalUrl",
  "startsWith": "",
  "applyListingsFilter": false,
  "applyOfficesFilter": false,
  "applyAgentsFilter": false
}
(.venv) WebCrawlers % jq '.log.entries[] | select(.request.url == "https://www.bhgre.com/api/places/search") | .response.content.text | fromjson | .data.results[] | select(.displayName | contains("New Jersey"))' ~/Downloads/www.bhgre.com.har
{
  "_id": "P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e",
  "dataSources": [
    "attom"
  ],
  "placeMasterId": "P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e",
  "placeName": "New Jersey",
  "canonicalUrl": "/state/nj",
  "displayName": "New Jersey",
  "relatedPlaces": {},
  "boundary": {},
  "numberOfListings": 0,
  "numberOfIdxListings": 0,
  "hasOffices": false,
  "hasAgents": false,
  "hasAreasServedOffice": false,
  "hasRentals": false,
  "hasOpenHouse": false,
  "hasCondoTownhouse": false,
  "hasNewConstruction": false,
  "hasLand": false,
  "hasListings": false
}
(.venv) WebCrawlers % python3 -c "from scrapy_crawlers.spiders.bhgre_spider import BhgreSpider; print('✓ Spider imported successfully'); print(f'Spider name: {BhgreSpider.name}'); print(f'NJ Place ID: {BhgreSpider.NJ_PLACE_ID}')"
✓ Spider imported successfully
Spider name: bhgre
NJ Place ID: P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e

```


```

Key endpoints discovered:
- `https://www.bhgre.com/api/listings` - Main listings API (POST)
- `https://www.bhgre.com/api/places/search` - Places search API (POST)
- `https://www.bhgre.com/api/places` - Places info API (GET)

#### 3. Analyzing the Listings API

The main listings API uses POST requests with JSON payloads. We extracted the request structure:

```bash
# Get listings API request details
jq '.log.entries[] | select(.request.url | contains("api/listings")) | {url: .request.url, method: .request.method, status: .response.status}' ~/Downloads/www.bhgre.com.har
```

**Request Body Structure:**
```json
{
  "ctx": {
    "brandCode": "BHG",
    "language": "en-US"
  },
  "numPerPage": 300,
  "page": 1,
  "status": "ACTIVE,PENDING,COMING_SOON",
  "showMlsListings": true,
  "minNumImages": 0,
  "projectedFields": "projectedFields.UniversalPlatform",
  "placeMasterIds": "P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e",
  "viewBoundary": {
    "topRightMapPoint": [-73.89, 41.35],
    "bottomLeftMapPoint": [-74.75, 38.93]
  },
  "propertyType": "SFR,MFR,MFD,CONDO,TOWNHOUSE,COOP,LAND,FARM",
  "sortBy": "[{\"key\":\"newListingTimeStamp\",\"order\":\"DESC\"}]"
}
```

#### 4. Finding New Jersey Place ID

To target New Jersey specifically, we searched for the state in the places API:

```bash
# Find New Jersey place ID
jq '.log.entries[] | select(.request.url == "https://www.bhgre.com/api/places/search") | .response.content.text | fromjson | .data.results[] | select(.displayName | contains("New Jersey"))' ~/Downloads/www.bhgre.com.har
```

**New Jersey Place ID:** `P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e`

#### 5. Understanding Response Structure

We analyzed the API response to understand the data structure:

```bash
# Get response structure
jq '.log.entries[] | select(.request.url | contains("api/listings")) | .response.content.text | fromjson | .data | keys' ~/Downloads/www.bhgre.com.har

# Get sample listing
jq '.log.entries[] | select(.request.url | contains("api/listings")) | .response.content.text | fromjson | .data.results[0]' ~/Downloads/www.bhgre.com.har | head -50
```

**Response Structure:**
- `data.results[]` - Array of property listings
- `data.pagination` - Pagination information
- Each listing contains: id, location, property details, pricing, photos, etc.

#### 6. Creating the BHGRE Spider

Based on the HAR analysis, we created `scrapy_crawlers/spiders/bhgre_spider.py` with the following features:

- **API Integration:** Makes POST requests to the listings API
- **Pagination:** Automatically handles multiple pages of results
- **Comprehensive Data Extraction:** Extracts all available property information
- **New Jersey Focus:** Targets the entire state using the correct place ID and boundaries

### Spider Features

#### Data Extracted
- **Basic Info:** Property ID, URL, price, type, status
- **Location:** Address, city, state, postal code, coordinates
- **Property Details:** Bedrooms, bathrooms, area, lot size
- **MLS Information:** MLS number, listing office, agent
- **Photos:** Photo count and URLs
- **Open Houses:** Scheduled open house times
- **Additional Flags:** Luxury status, image availability

#### Configuration
- **Place ID:** `P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e` (New Jersey)
- **Boundaries:** Covers entire NJ state geographically
- **Property Types:** All residential types (SFR, condo, townhouse, etc.)
- **Status Types:** Active, pending, and coming soon listings
- **Pagination:** 300 properties per page

### Usage

#### Running the Spider

1. **Navigate to the project directory:**
   ```bash
   cd WebCrawlers
   ```

2. **Run the spider:**
   ```bash
   scrapy crawl bhgre
   ```

3. **Save output to JSON:**
   ```bash
   scrapy crawl bhgre -o nj_properties.json
   ```

4. **Save output to CSV:**
   ```bash
   scrapy crawl bhgre -o nj_properties.csv
   ```

#### Output Format

The spider yields items with the following structure:

```json
{
  "id": "P00800000HABAHtaBEZliPMeNEAE0iJCuzqXEFcQ",
  "url": "/nj/mahwah/30-n-bayard-ln/lid-P00800000HABAHtaBEZliPMeNEAE0iJCuzqXEFcQ",
  "price": 799999,
  "property_type": "TOWNHOUSE",
  "status": "ACTIVE",
  "address": "30 N Bayard Ln",
  "city": "Mahwah Twp.",
  "state": "NJ",
  "postal_code": "07430",
  "latitude": 41.08041,
  "longitude": -74.134213,
  "bedrooms": 2,
  "bathrooms": 4,
  "listing_area": null,
  "lot_size": 0.09,
  "lot_size_units": "Acres",
  "mls_number": "4024260",
  "listing_office": "TERRIE O'CONNOR REALTORS",
  "agent_name": "JOSEPH O CONNOR",
  "photos_count": 0,
  "first_photo_url": null,
  "open_houses": [
    {
      "startTime": "2026-05-02T12:00:00.000Z",
      "endTime": "2026-05-02T15:00:00.000Z"
    }
  ],
  "is_luxury": false,
  "has_images": false,
  "raw_listing": {...}
}
```

### Technical Details

#### Dependencies
- Scrapy 2.15.2
- Python 3.11+
- curl_cffi (for browser-grade TLS fingerprinting)

#### Settings
The spider uses the project's Scrapy settings including:
- Custom curl_cffi download handler
- User agent rotation
- Retry middleware
- Rate limiting (10 second delay, 4 concurrent requests per domain)

#### Error Handling
- JSON parsing errors are logged
- Network failures are handled by Scrapy's retry middleware
- Invalid responses are skipped with error logging

#### Files
- `scrapy_crawlers/spiders/bhgre_spider.py` - Main spider implementation
- `scrapy_crawlers/settings/settings.py` - Scrapy configuration
- `requirements.txt` - Python dependencies

#### Notes
- The spider respects BHGRE's robots.txt and implements appropriate delays
- All data extraction is based on the public API responses
- The spider is designed to be maintainable and can be easily adapted for other states or regions

---

## GSMLS Spider

The GSMLS spider is implemented at `scrapy_crawlers/spiders/gsmls_spider.py` and follows the public multi-step flow:

1. County selection (`getcountysearch`)
2. Town selection (`getcommsearch`)
3. Criteria page (`getpropertysearch`)
4. Results page (`getpropertydetails`)

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
