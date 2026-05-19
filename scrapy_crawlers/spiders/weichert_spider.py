import json
import re
from urllib.parse import urljoin, urlparse

import scrapy

from spiders.env_config import build_proxy_url, get_env

BEDS_PATTERN = re.compile(r"(\d+)")
FULL_BATHS_PATTERN = re.compile(r"(\d+)\s*full\s*bath", re.I)
HALF_BATHS_PATTERN = re.compile(r"(\d+)\s*half\s*bath", re.I)
NUMBER_PATTERN = re.compile(r"-?[\d,.]+")
CITY_ID_PATTERN = re.compile(r"cityid=([0-9,]+)", re.I)
DETAIL_IMAGE_PATTERN = re.compile(
    r'(?:https?:)?//d36xftgacqn2p\.cloudfront\.net/[^"\']+\.(?:jpg|jpeg|png|webp)',
    re.I,
)


class WeichertSpider(scrapy.Spider):
    name = "weichert"
    allowed_domains = ["www.weichert.com"]
    handle_httpstatus_list = [400, 401, 403, 429]

    WARMUP_URL = "https://www.weichert.com/NJ/"
    NJ_CITIES_URL = "https://www.weichert.com/NJ/cities/"
    SEARCH_API_URL = "https://www.weichert.com/api/search"
    # New Jersey rough bounds: south, west, north, east.
    NJ_BBOX = (38.88, -75.62, 41.37, -73.89)

    custom_settings = {
        "COOKIES_ENABLED": True,
        "DOWNLOAD_DELAY": 2,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "CURL_IMPERSONATE": "chrome110",
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json; charset=UTF-8",
            "dnt": "1",
            "origin": "https://www.weichert.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {"1", "true", "yes"}
        self.proxy_url = None if disable_proxy else build_proxy_url()
        self.max_pages_per_query = self._safe_int(
            kwargs.get("max_pages_per_query"),
            get_env("WEICHERT_MAX_PAGES_PER_QUERY", default="50"),
            fallback=50,
        )
        self.max_towns = self._safe_int(
            kwargs.get("max_towns"),
            get_env("WEICHERT_MAX_TOWNS"),
            fallback=None,
        )
        self.max_counties = self._safe_int(
            kwargs.get("max_counties"),
            get_env("WEICHERT_MAX_COUNTIES"),
            fallback=None,
        )
        self.enable_bbox_fallback = str(
            kwargs.get("enable_bbox_fallback", get_env("WEICHERT_ENABLE_BBOX_FALLBACK", default="true"))
        ).strip().lower() not in {"0", "false", "no"}
        self.bbox_rows = max(
            1,
            self._safe_int(kwargs.get("bbox_rows"), get_env("WEICHERT_BBOX_ROWS", default="5"), fallback=5),
        )
        self.bbox_cols = max(
            1,
            self._safe_int(kwargs.get("bbox_cols"), get_env("WEICHERT_BBOX_COLS", default="6"), fallback=6),
        )
        self.bbox_zoom = max(
            1,
            self._safe_int(kwargs.get("bbox_zoom"), get_env("WEICHERT_BBOX_ZOOM", default="10"), fallback=10),
        )
        self.bbox_overlap = self._to_float(
            kwargs.get("bbox_overlap", get_env("WEICHERT_BBOX_OVERLAP", default="0.02"))
        )
        if self.bbox_overlap is None or self.bbox_overlap < 0:
            self.bbox_overlap = 0.02
        self.seen_listing_ids = set()
        self.seen_search_queries = set()
        self.seen_town_urls = set()
        self.bbox_fallback_started = False
        self.county_fallback_pending = set()
        self.county_fallback_produced = False

    @staticmethod
    def _safe_int(value, default_value=None, fallback=None):
        candidate = value if value not in (None, "") else default_value
        try:
            return int(candidate) if candidate not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except Exception:
            try:
                return response.body.decode("utf-8", errors="replace")
            except Exception:
                return ""

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    async def start(self):
        if self.proxy_url:
            self.logger.info("Using proxy for Weichert requests")
        else:
            self.logger.warning("No proxy configured; running Weichert spider without proxy")
        yield scrapy.Request(
            self.WARMUP_URL,
            callback=self.parse_warmup,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def parse_warmup(self, response):
        warmup_text = self._safe_response_text(response)
        fallback_county_urls = []
        if response.status == 200 and warmup_text:
            fallback_county_urls = self._extract_nj_county_urls(warmup_text)
            if self.max_counties:
                fallback_county_urls = fallback_county_urls[: self.max_counties]

        self.logger.info(
            "Warmup status=%s max_pages_per_query=%s max_towns=%s max_counties=%s counties=%s bbox_fallback=%s grid=%sx%s zoom=%s",
            response.status,
            self.max_pages_per_query,
            self.max_towns,
            self.max_counties,
            len(fallback_county_urls),
            self.enable_bbox_fallback,
            self.bbox_rows,
            self.bbox_cols,
            self.bbox_zoom,
        )
        yield scrapy.Request(
            self.NJ_CITIES_URL,
            callback=self.parse_nj_cities_index,
            cb_kwargs={"fallback_county_urls": fallback_county_urls},
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def parse_nj_cities_index(self, response, fallback_county_urls=None):
        text = self._safe_response_text(response)
        if response.status != 200 or not text:
            self.logger.warning("Failed to load NJ cities index status=%s", response.status)
            yield from self._schedule_county_fallback(
                fallback_county_urls,
                reason=f"cities_index_status_{response.status}",
            )
            return

        selector = scrapy.Selector(text=text)
        hrefs = selector.xpath('//a[@href]/@href').getall()
        town_urls = []
        seen = set()
        for href in hrefs:
            full = self._to_absolute_url(href)
            if not full:
                continue
            if not self._is_nj_town_url(full):
                continue
            if full in seen:
                continue
            seen.add(full)
            town_urls.append(full)

        if not town_urls:
            self.logger.warning("NJ cities index returned zero town URLs; switching to county fallback")
            yield from self._schedule_county_fallback(
                fallback_county_urls,
                reason="cities_index_empty",
            )
            return

        if self.max_towns:
            town_urls = town_urls[: self.max_towns]

        self.logger.info("Discovered NJ town pages=%s", len(town_urls))

        for town_url in town_urls:
            request = self._build_town_request(town_url)
            if request:
                yield request

    def _schedule_county_fallback(self, county_urls, reason):
        urls = county_urls or []
        if not urls:
            self.logger.warning("County fallback unavailable; no county URLs found (%s)", reason)
            yield from self._schedule_bbox_fallback(reason=f"county_unavailable_{reason}")
            return

        self.logger.info("Using county fallback (%s) counties=%s", reason, len(urls))
        self.county_fallback_pending = {self._canonical_url(u) for u in urls}
        self.county_fallback_produced = False
        for county_url in urls:
            yield scrapy.Request(
                county_url,
                callback=self.parse_county_page,
                meta=self._proxy_meta(),
                dont_filter=True,
            )

    def parse_county_page(self, response):
        produced = False
        county, _ = self._parse_county_town_from_url(response.url)
        text = self._safe_response_text(response)
        if response.status != 200 or not text:
            self.logger.warning("County page failed county=%s status=%s url=%s", county, response.status, response.url)
            yield from self._finish_county_fallback(response.url, produced=False)
            return

        selector = scrapy.Selector(text=text)
        hrefs = selector.xpath('//a[@href]/@href').getall()
        town_urls = []
        seen = set()
        for href in hrefs:
            full = self._to_absolute_url(href)
            if not full or not self._is_nj_town_url(full) or full in seen:
                continue
            seen.add(full)
            town_urls.append(full)

        if self.max_towns:
            remaining = max(self.max_towns - len(self.seen_town_urls), 0)
            if remaining == 0:
                self.logger.info("Skipping county=%s because max_towns cap is already reached", county)
                yield from self._finish_county_fallback(response.url, produced=False)
                return
            town_urls = town_urls[:remaining]

        scheduled = 0
        for town_url in town_urls:
            request = self._build_town_request(town_url)
            if request:
                scheduled += 1
                yield request
        if scheduled:
            produced = True

        self.logger.info(
            "County fallback county=%s town_candidates=%s scheduled=%s",
            county,
            len(town_urls),
            scheduled,
        )

        if scheduled:
            yield from self._finish_county_fallback(response.url, produced=produced)
            return

        # Final fallback: run county-level query if town links are missing.
        query = selector.xpath('normalize-space(//*[@id="searchresults"]/@data-searchquery)').get()
        query = self._normalize_search_query(query)
        if not query:
            match = CITY_ID_PATTERN.search(text)
            if match:
                query = self._normalize_search_query(f"cityid={match.group(1)}")

        if not query:
            self.logger.warning("No county fallback query found county=%s url=%s", county, response.url)
            yield from self._finish_county_fallback(response.url, produced=produced)
            return

        if query in self.seen_search_queries:
            yield from self._finish_county_fallback(response.url, produced=produced)
            return
        self.seen_search_queries.add(query)
        produced = True

        yield self.search_request(
            search_query=query,
            page=1,
            county=county,
            town=None,
            referer=response.url,
        )
        yield from self._finish_county_fallback(response.url, produced=produced)

    def parse_town_page(self, response, county, town):
        text = self._safe_response_text(response)
        if response.status != 200 or not text:
            self.logger.warning("Town page failed county=%s town=%s status=%s", county, town, response.status)
            return

        selector = scrapy.Selector(text=text)
        query = selector.xpath('normalize-space(//*[@id="searchresults"]/@data-searchquery)').get()
        query = self._normalize_search_query(query)

        if not query:
            match = CITY_ID_PATTERN.search(text)
            if match:
                query = self._normalize_search_query(f"cityid={match.group(1)}")

        if not query:
            self.logger.warning("No city search query found county=%s town=%s url=%s", county, town, response.url)
            return

        if query in self.seen_search_queries:
            return
        self.seen_search_queries.add(query)

        yield self.search_request(
            search_query=query,
            page=1,
            county=county,
            town=town,
            referer=response.url,
        )

    def search_request(self, search_query, page, county, town, referer):
        current_search = f"{search_query}&pg={page}"
        payload = {
            "redirectRequired": False,
            "currentSearch": current_search,
            "location": None,
            "form": None,
        }
        return scrapy.Request(
            self.SEARCH_API_URL,
            method="POST",
            body=json.dumps(payload),
            callback=self.parse_search,
            headers={"referer": referer or self.WARMUP_URL},
            meta={
                "query": search_query,
                "query_page": page,
                "county": county,
                "town": town,
                "query_referer": referer or self.WARMUP_URL,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def parse_search(self, response):
        page = response.meta.get("query_page", 1)
        search_query = response.meta.get("query") or ""
        county = response.meta.get("county")
        town = response.meta.get("town")
        referer = response.meta.get("query_referer", self.WARMUP_URL)

        response_text = self._safe_response_text(response)
        if response.status != 200:
            self.logger.warning(
                "Weichert search failed status=%s query=%s page=%s county=%s town=%s body=%s",
                response.status,
                search_query,
                page,
                county,
                town,
                response_text[:300],
            )
            return

        try:
            payload = json.loads(response_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "Failed to parse Weichert JSON query=%s page=%s err=%s",
                search_query,
                page,
                exc,
            )
            return

        listings = payload.get("listings") or []
        total_pages = self._safe_int(payload.get("pages"), fallback=1) or 1
        total_listings = self._to_int(payload.get("totalListings"))
        result_set_total = self._to_int(payload.get("resultSetTotal"))

        self.logger.info(
            "Weichert query=%s page=%s county=%s town=%s listings=%s totalListings=%s resultSetTotal=%s totalPages=%s",
            search_query,
            page,
            county,
            town,
            len(listings),
            total_listings,
            result_set_total,
            total_pages,
        )

        yielded_count = 0
        for listing in listings:
            item = self.parse_listing_item(listing, page=page, county=county)
            if not item:
                continue
            dedupe_id = item.get("listing_id") or item.get("mls_id") or item.get("detail_url")
            if not dedupe_id or dedupe_id in self.seen_listing_ids:
                continue
            self.seen_listing_ids.add(dedupe_id)
            yielded_count += 1
            detail_url = item.get("detail_url")
            if detail_url:
                yield scrapy.Request(
                    detail_url,
                    callback=self.parse_detail_page,
                    meta={"base_item": item, **self._proxy_meta()},
                    dont_filter=True,
                )
            else:
                yield item

        if listings and page < min(total_pages, self.max_pages_per_query):
            yield self.search_request(
                search_query=search_query,
                page=page + 1,
                county=county,
                town=town,
                referer=referer,
            )
        elif listings and total_pages > self.max_pages_per_query:
            self.logger.info(
                "Page cap hit query=%s total_pages=%s max_pages_per_query=%s",
                search_query,
                total_pages,
                self.max_pages_per_query,
            )

        self.logger.info(
            "Weichert yielded_new=%s query=%s page=%s county=%s town=%s",
            yielded_count,
            search_query,
            page,
            county,
            town,
        )

    def parse_listing_item(self, listing, page, county):
        if not isinstance(listing, dict):
            return None

        listing_id = listing.get("p")
        mls_id = listing.get("mls")
        detail_path = listing.get("url")
        detail_url = self._to_absolute_url(detail_path)

        lot_size_acres = self._to_float(listing.get("lot"))
        lot_size_sqft = None
        if lot_size_acres is not None:
            lot_size_sqft = int(round(lot_size_acres * 43560))

        build_area_sqft = self._to_int(listing.get("sqft"))
        beds = self._extract_beds(listing.get("beds"))
        baths = self._extract_total_baths(listing)
        photo_links = self._extract_photo_links_from_listing(listing)

        return {
            "source": "weichert",
            "county": self._clean_str(county),
            "region_id": None,
            "listing_id": str(listing_id).strip() if listing_id not in (None, "") else None,
            "mls_id": str(mls_id).strip() if mls_id not in (None, "") else None,
            "detail_url": detail_url,
            "address": self._clean_str(listing.get("addr")),
            "city": self._clean_str(listing.get("city")),
            "state": self._clean_str(listing.get("state")),
            "postal_code": self._clean_str(listing.get("zip")),
            "list_price": self._to_int(listing.get("price")),
            "status": self._normalize_status(listing.get("saletype")),
            "property_type": self._clean_str(listing.get("type")),
            "beds": beds,
            "baths": baths,
            "lot_size_sqft": lot_size_sqft,
            "lot_size_acres": lot_size_acres,
            "build_area_sqft": build_area_sqft,
            "year_built": self._to_int(listing.get("year")),
            "stories": None,
            "latitude": self._to_float(listing.get("lat")),
            "longitude": self._to_float(listing.get("lng")),
            "description": self._clean_str(listing.get("description")),
            "heating": None,
            "cooling": None,
            "appliances": None,
            "flooring": None,
            "photos_count": len(photo_links),
            "first_photo_url": photo_links[0] if photo_links else None,
            "photo_links": photo_links,
            "page": page,
        }

    def parse_detail_page(self, response):
        item = dict(response.meta.get("base_item") or {})
        if response.status != 200:
            yield item
            return

        text = self._safe_response_text(response)
        if not text:
            yield item
            return

        selector = scrapy.Selector(text=text)
        heating = self._extract_feature_values(selector, "Heating")
        cooling = self._extract_feature_values(selector, "Cooling")
        appliances = self._extract_feature_values(selector, "Appliances")
        flooring = self._extract_feature_values(selector, "Flooring")
        if not item.get("photo_links"):
            detail_links = self._extract_photo_links_from_detail_html(text)
            if detail_links:
                item["photo_links"] = detail_links
                item["photos_count"] = len(detail_links)
                item["first_photo_url"] = detail_links[0]

        if heating:
            item["heating"] = heating
        if cooling:
            item["cooling"] = cooling
        if appliances:
            item["appliances"] = appliances
        if flooring:
            item["flooring"] = flooring

        yield item

    def _extract_photo_links_from_listing(self, listing):
        links = []
        seen = set()

        def add(value):
            normalized = self._normalize_photo_url(value)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            links.append(normalized)

        images = listing.get("images")
        if isinstance(images, list):
            for value in images:
                add(value)

        add(listing.get("img"))
        add(listing.get("thumb"))
        return links

    def _extract_photo_links_from_detail_html(self, html):
        links = []
        seen = set()
        for match in DETAIL_IMAGE_PATTERN.findall(html or ""):
            normalized = self._normalize_photo_url(match)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            links.append(normalized)
        return links

    @staticmethod
    def _normalize_photo_url(value):
        text = (str(value or "").strip())
        if not text:
            return None
        if text.startswith("//"):
            return f"https:{text}"
        if text.startswith("http://") or text.startswith("https://"):
            return text
        if text.startswith("/"):
            return urljoin("https://www.weichert.com", text)
        return None

    def _extract_feature_values(self, selector, feature_name):
        values = []
        nodes = selector.xpath(
            '//div[contains(@class, "property-feature-listing")]'
            '[.//span[contains(@class, "feature-subcateory-heading") and '
            'normalize-space()=$feature]]'
            '//span[contains(@class, "listing-feature-items")]/text()',
            feature=feature_name,
        ).getall()
        for raw in nodes:
            cleaned = self._clean_str(raw)
            if cleaned and cleaned not in values:
                values.append(cleaned)
        if values:
            return ", ".join(values)
        return None

    @staticmethod
    def _normalize_search_query(query):
        value = (query or "").strip().strip("&")
        if not value:
            return None
        # Remove page component if present; paging is controlled separately.
        value = re.sub(r"(?:^|&)pg=\d+(?:&|$)", "&", value, flags=re.I).strip("&")
        value = re.sub(r"&{2,}", "&", value)
        return value or None

    @staticmethod
    def _is_nj_town_url(url):
        parsed = urlparse(url)
        if parsed.netloc not in {"www.weichert.com", "weichert.com"}:
            return False
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 3:
            return False
        if parts[0].upper() != "NJ":
            return False
        if parts[1].lower() == "cities":
            return False
        return True

    @staticmethod
    def _is_nj_county_url(url):
        parsed = urlparse(url)
        if parsed.netloc not in {"www.weichert.com", "weichert.com"}:
            return False
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2:
            return False
        if parts[0].upper() != "NJ":
            return False
        if parts[1].lower() == "cities":
            return False
        return True

    def _extract_nj_county_urls(self, text):
        selector = scrapy.Selector(text=text)
        hrefs = selector.xpath('//a[@href]/@href').getall()
        county_urls = []
        seen = set()
        for href in hrefs:
            full = self._to_absolute_url(href)
            if not full or not self._is_nj_county_url(full) or full in seen:
                continue
            seen.add(full)
            county_urls.append(full)
        return county_urls

    def _build_town_request(self, town_url):
        if town_url in self.seen_town_urls:
            return None
        self.seen_town_urls.add(town_url)
        county, town = self._parse_county_town_from_url(town_url)
        return scrapy.Request(
            town_url,
            callback=self.parse_town_page,
                cb_kwargs={"county": county, "town": town},
                meta=self._proxy_meta(),
            dont_filter=True,
        )

    def _finish_county_fallback(self, county_url, produced):
        if produced:
            self.county_fallback_produced = True

        canonical = self._canonical_url(county_url)
        if canonical in self.county_fallback_pending:
            self.county_fallback_pending.remove(canonical)

        if self.county_fallback_pending:
            return

        if not self.county_fallback_produced:
            yield from self._schedule_bbox_fallback(reason="county_fallback_no_queries")

    def _schedule_bbox_fallback(self, reason):
        if not self.enable_bbox_fallback:
            self.logger.info("BBox fallback disabled (%s)", reason)
            return
        if self.bbox_fallback_started:
            return

        self.bbox_fallback_started = True
        south, west, north, east = self.NJ_BBOX
        lat_step = (north - south) / float(self.bbox_rows)
        lon_step = (east - west) / float(self.bbox_cols)
        tile_count = 0

        self.logger.warning(
            "Starting bbox fallback (%s): rows=%s cols=%s zoom=%s overlap=%s",
            reason,
            self.bbox_rows,
            self.bbox_cols,
            self.bbox_zoom,
            self.bbox_overlap,
        )

        for row in range(self.bbox_rows):
            for col in range(self.bbox_cols):
                tile_south = south + (row * lat_step)
                tile_north = south + ((row + 1) * lat_step)
                tile_west = west + (col * lon_step)
                tile_east = west + ((col + 1) * lon_step)

                # Add slight overlap to reduce edge-loss from map clustering/windowing.
                tile_south = max(south, tile_south - self.bbox_overlap)
                tile_north = min(north, tile_north + self.bbox_overlap)
                tile_west = max(west, tile_west - self.bbox_overlap)
                tile_east = min(east, tile_east + self.bbox_overlap)

                query = (
                    f"bounds={tile_south:.8f},{tile_west:.8f},{tile_north:.8f},{tile_east:.8f}"
                    f"&zoom={self.bbox_zoom}"
                )
                query = self._normalize_search_query(query)
                if not query or query in self.seen_search_queries:
                    continue

                self.seen_search_queries.add(query)
                tile_count += 1
                yield self.search_request(
                    search_query=query,
                    page=1,
                    county=None,
                    town="__bbox__",
                    referer=self.WARMUP_URL,
                )

        self.logger.warning("BBox fallback scheduled tiles=%s", tile_count)

    @staticmethod
    def _parse_county_town_from_url(url):
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        county = parts[1].replace("_", " ") if len(parts) >= 2 else None
        town = parts[2].replace("_", " ") if len(parts) >= 3 else None
        return county, town

    @staticmethod
    def _to_absolute_url(url_or_path):
        value = (url_or_path or "").strip()
        if not value:
            return None
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("//"):
            return f"https:{value}"
        if value.startswith("/"):
            return urljoin("https://www.weichert.com", value)
        return urljoin("https://www.weichert.com/", value)

    @staticmethod
    def _canonical_url(url):
        absolute = WeichertSpider._to_absolute_url(url)
        if not absolute:
            return None
        return absolute.rstrip("/")

    @staticmethod
    def _clean_str(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _normalize_status(raw):
        text = (str(raw).strip() if raw is not None else "")
        if not text:
            return None
        return text.replace("_", " ").upper()

    @staticmethod
    def _to_int(value):
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                return int(round(float(value)))
            except (TypeError, ValueError):
                return None
        match = NUMBER_PATTERN.search(str(value))
        if not match:
            return None
        try:
            return int(round(float(match.group(0).replace(",", ""))))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value):
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        match = NUMBER_PATTERN.search(str(value))
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _extract_beds(self, value):
        text = self._clean_str(value)
        if not text:
            return None
        m = BEDS_PATTERN.search(text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None

    def _extract_total_baths(self, listing):
        baths_text = self._clean_str(listing.get("baths"))
        if baths_text:
            full_match = FULL_BATHS_PATTERN.search(baths_text)
            half_match = HALF_BATHS_PATTERN.search(baths_text)
            full_baths = int(full_match.group(1)) if full_match else 0
            half_baths = int(half_match.group(1)) if half_match else 0
            if full_match or half_match:
                return float(full_baths) + (0.5 * float(half_baths))

        short_text = self._clean_str(listing.get("bathsshort"))
        if not short_text:
            return None

        m = re.search(r"(\d+)\.(\d+)", short_text)
        if m:
            try:
                whole = int(m.group(1))
                frac = int(m.group(2))
            except (TypeError, ValueError):
                whole = frac = None
            if whole is not None and frac is not None:
                return float(whole) + (0.5 * float(frac))

        return self._to_float(short_text)
