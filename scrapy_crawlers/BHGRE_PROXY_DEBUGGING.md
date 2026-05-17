# BHGRE Proxy 407 Debugging Runbook

This document captures the debugging process used to resolve:

`curl: (56) CONNECT tunnel failed, response 407`

for the `bhgre` spider.

## 1) Symptom

Spider failed on warmup request:

- `GET https://www.bhgre.com/home/list/county/nj/bergen-county`
- Error: `ProxyError ... CONNECT tunnel failed, response 407`

## 2) Confirm `.env` is loaded

Check `.env` values:

```bash
sed -n '1,220p' proxies/settings/.env
```

Validate proxy URL is built from env:

```bash
cd proxies
../.venv/bin/python - <<'PY'
from spiders.env_config import build_proxy_url
u = build_proxy_url()
print('configured' if u else 'missing')
print('host_ok', '@gw.dataimpulse.com:' in u if u else False)
PY
```

Expected: `configured` and `host_ok True`.

## 3) Check whether credentials work outside Scrapy

Direct proxy test:

```bash
cd proxies
../.venv/bin/python - <<'PY'
from spiders.env_config import build_proxy_url
from curl_cffi.requests import Session

proxy = build_proxy_url()
with Session(impersonate='chrome124', verify=True) as s:
    r = s.get('https://api.ipify.org?format=json', proxies={'http': proxy, 'https': proxy}, timeout=20)
    print(r.status_code, r.text[:120])
PY
```

If this returns `200`, credentials are valid.

Also test BHGRE directly:

```bash
cd proxies
../.venv/bin/python - <<'PY'
from spiders.env_config import build_proxy_url
from curl_cffi.requests import Session

proxy = build_proxy_url()
with Session(impersonate='chrome124', verify=True) as s:
    r = s.get('https://www.bhgre.com/home/list/county/nj/bergen-county', proxies={'http': proxy, 'https': proxy}, timeout=25)
    print(r.status_code, len(r.content))
PY
```

If this returns `200`, provider + target domain are reachable through proxy.

## 4) Isolate Scrapy pipeline issue

Run spider and observe 407:

```bash
cd proxies
../.venv/bin/scrapy crawl bhgre -s LOG_LEVEL=INFO -s TELNETCONSOLE_ENABLED=False -s CLOSESPIDER_PAGECOUNT=2 -O /tmp/bhgre_debug.csv
```

Since direct proxy tests were `200` but Scrapy failed `407`, issue is middleware interaction.

## 5) Root cause

`HttpProxyMiddleware` was active and rewriting proxy auth behavior:

- It stripped credentials from `request.meta['proxy']`.
- It moved auth to `Proxy-Authorization`.
- Custom `curl_cffi` download handler authenticates via proxy URL (`kwargs["proxies"]`).
- Result: handler saw unauthenticated proxy URL and got `407`.

## 6) Fix

Disable Scrapy `HttpProxyMiddleware` in `scrapy_crawlers/settings/settings.py`:

```python
DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': 400,
    'scrapy.downloadermiddlewares.retry.RetryMiddleware': 405,
    'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': None,
    'scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware': None,
}
```

## 7) Verification

Re-run:

```bash
cd proxies
../.venv/bin/scrapy crawl bhgre -s LOG_LEVEL=INFO -s TELNETCONSOLE_ENABLED=False -s CLOSESPIDER_PAGECOUNT=2 -O /tmp/bhgre_proxy_fixed.csv
```

Expected:

- warmup `200`
- listings API `200`
- items exported (example run: `300` items)

## 8) Notes

- `.env` loading is handled by `spiders/env_config.py`.
- If DataImpulse credentials change, update `.env` values:
  - `PROXY_HOST`
  - `PROXY_PORT`
  - `LOGIN` (or `PROXY_USERNAME`)
  - `PASSWORD` (or `PROXY_PASSWORD`)
