# Realtor Spider Debugging Runbook

This runbook documents how to debug and validate the Realtor spider built from:

`~/Downloads/www.realtor.com.har`

## 1) Primary symptoms and what they mean

- `400 missing client identification headers` on `POST /frontdoor/graphql`
  - Missing required Realtor client headers (see section 3).
- `429` on warmup page (`GET /realestateandhomes-search/New-Jersey`)
  - Can happen under anti-bot controls; GraphQL may still work.
- Parser exceptions (e.g. `NoneType has no attribute get`)
  - Realtor response fields can be `null` in nested objects.

## 2) Confirm HAR has usable listing traffic

List operation names used on GraphQL:

```bash
jq -r '.log.entries[]
  | select(.request.url == "https://www.realtor.com/frontdoor/graphql")
  | .request.postData.text
  | fromjson
  | .operationName' ~/Downloads/www.realtor.com.har | sort | uniq -c
```

Look for `ConsumerSearchQuery` with sale statuses.

## 3) Required GraphQL headers (from HAR)

Realtor GraphQL expects these client identification headers:

- `rdc-ab-test-client: rdc-search-for-sale`
- `rdc-client-name: RDC_WEB_SRP_FS_PAGE`
- `rdc-client-version: 3.0.2798`
- `x-rdc-visitor-id: <uuid>`
- `x-is-bot: false`

Plus standard browser CORS headers (`origin`, `referer`, `sec-fetch-*`, `user-agent`, etc.).

Without these, API returns:

`400 missing client identification headers`

## 4) Extract exact request body from HAR

```bash
jq -r '.log.entries[]
  | select(.request.url == "https://www.realtor.com/frontdoor/graphql")
  | .request.postData.text
  | fromjson
  | select(.operationName == "ConsumerSearchQuery")
  | .variables' ~/Downloads/www.realtor.com.har
```

Use the `for_sale` + `ready_to_build` variant for listing crawl.

## 5) Validate endpoint outside Scrapy first

Run a direct probe with `curl_cffi` and HAR headers.

Expected:
- warmup may be `429`
- GraphQL should be `200`
- `data.home_search.properties` should contain listings

## 6) Pagination behavior

For `ConsumerSearchQuery`:

- set `limit=42`
- start `offset=0`
- increment by returned `count`
- stop when `offset >= total`

## 7) Null-safe parsing is required

Common nullable fields:

- `location`
- `location.address`
- `location.address.coordinate`
- `location.county`
- `advertisers[*].phones`

Always guard nested dict/list access with `or {}` / `or []` and type checks.

## 8) Warmup strategy

Warmup request:

`GET https://www.realtor.com/realestateandhomes-search/New-Jersey`

Use `dont_retry=True` on warmup request metadata to avoid wasting retries on repeated `429` when GraphQL still succeeds.

## 9) Proxy checks (DataImpulse)

Confirm env is loaded and proxy URL is built:

```bash
cd proxies
../.venv/bin/python - <<'PY'
from spiders.env_config import build_proxy_url
print(bool(build_proxy_url()))
PY
```

Quick outbound proxy test:

```bash
cd proxies
../.venv/bin/python - <<'PY'
from spiders.env_config import build_proxy_url
from curl_cffi.requests import Session

proxy = build_proxy_url()
with Session(impersonate='chrome124', verify=True) as s:
    r = s.get('https://api.ipify.org?format=json', proxies={'http': proxy, 'https': proxy}, timeout=20)
    print(r.status_code, r.text)
PY
```

## 10) Verification commands

Smoke run:

```bash
cd proxies
../.venv/bin/scrapy crawl realtor \
  -s LOG_LEVEL=INFO \
  -s TELNETCONSOLE_ENABLED=False \
  -s CLOSESPIDER_PAGECOUNT=3 \
  -O /tmp/realtor_smoke.csv
```

Successful indicators:

- GraphQL responses are `200`
- Log lines like `Realtor page offset=0 ...` then `offset=42 ...`
- Non-zero `item_scraped_count`

## 11) Files involved

- Spider: `scrapy_crawlers/spiders/realtor_spider.py`
- Env/proxy loader: `scrapy_crawlers/spiders/env_config.py`
- Project settings: `scrapy_crawlers/settings/settings.py`
