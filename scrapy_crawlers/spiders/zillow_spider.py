import json
import re
from copy import deepcopy

import scrapy

from spiders.env_config import build_proxy_url


class ZillowSpider(scrapy.Spider):
    name = "zillow"
    allowed_domains = ["www.zillow.com"]
    handle_httpstatus_list = [202, 400, 401, 403, 429]

    WARMUP_URL = "https://www.zillow.com/nj/"
    BROWSE_URL = "https://www.zillow.com/browse/homes/nj/"
    SEARCH_API_URL = "https://www.zillow.com/async-create-search-page-state"
    MAX_SAFE_PAGE_PER_QUERY = 4
    MAX_PAGES_PER_QUERY = 20
    MAX_RESULTS_PER_QUERY = 160
    MAX_SPLIT_DEPTH = 4
    MIN_LAT_SPAN = 0.01
    MIN_LON_SPAN = 0.01
    NJ_REGION_BOUNDS = {
        "north": 41.357423,
        "south": 38.788657,
        "east": -73.88506,
        "west": -75.563586,
    }

    BASE_SEARCH_QUERY_STATE = {
        "isMapVisible": True,
        "mapBounds": NJ_REGION_BOUNDS,
        "usersSearchTerm": "new jersey",
        "filterState": {"sortSelection": {"value": "globalrelevanceex"}},
        "isListVisible": True,
        "mapZoom": 7,
        "regionSelection": [{"regionId": 40, "regionType": 2}],
        "category": "cat1",
        "pagination": {"currentPage": 1},
    }

    BASE_WANTS = {
        "cat1": ["listResults", "mapResults"],
        "cat2": ["total"],
        "abTrials": ["total"],
    }

    NJ_COUNTIES = [
        "Atlantic",
        "Bergen",
        "Burlington",
        "Camden",
        "Cape May",
        "Cumberland",
        "Essex",
        "Gloucester",
        "Hudson",
        "Hunterdon",
        "Mercer",
        "Middlesex",
        "Monmouth",
        "Morris",
        "Ocean",
        "Passaic",
        "Salem",
        "Somerset",
        "Sussex",
        "Union",
        "Warren",
    ]

    COUNTY_LABEL_PATTERN = re.compile(r"^\s*(.+?)\s+County\s+NJ\s*$", re.IGNORECASE)
    CITY_LABEL_SUFFIX = " Real Estate"
    PHONE_PATTERN = re.compile(
        r"(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?:\s*(?:x|ext\.?)\s*\d{1,5})?",
        re.IGNORECASE,
    )
    EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

    custom_settings = {
        "COOKIES_ENABLED": True,
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "dnt": "1",
            "origin": "https://www.zillow.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        },
        "DOWNLOAD_DELAY": 2,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "CURL_IMPERSONATE": "chrome110",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = build_proxy_url()
        self.seen_zpids = set()
        self.seen_search_keys = set()
        self.city_queries_scheduled = 0
        self.county_queries_scheduled = 0
        self.max_cities = self._safe_int(kwargs.get("max_cities"), fallback=None)
        self.max_counties = self._safe_int(kwargs.get("max_counties"), fallback=None)
        self.strategy_queries = {"city": 0, "county": 0, "bbox": 0}
        self.strategy_result_pages = {"city": 0, "county": 0, "bbox": 0}
        self.strategy_empty_pages = {"city": 0, "county": 0, "bbox": 0}
        self.strategy_items = {"city": 0, "county": 0, "bbox": 0}
        self.contact_enrichment_counts = {
            "detail_records": 0,
            "listing_agent": 0,
            "listing_office_phone": 0,
            "listing_agent_phone": 0,
            "listing_agent_email": 0,
            "listing_office_email": 0,
        }

    @staticmethod
    def _safe_int(value, fallback=None):
        try:
            return int(value) if value not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    @staticmethod
    def _normalize_strategy(strategy):
        value = (strategy or "bbox").strip().lower()
        if value not in {"city", "county", "bbox"}:
            return "bbox"
        return value

    def _bump_strategy_counter(self, counter_name, strategy, increment=1):
        normalized = self._normalize_strategy(strategy)
        bucket = getattr(self, counter_name, None)
        if not isinstance(bucket, dict):
            return
        bucket[normalized] = int(bucket.get(normalized, 0)) + int(increment)

    async def start(self):
        if self.proxy_url:
            self.logger.info("Using DataImpulse rotating proxy for Zillow requests")
        else:
            self.logger.warning(
                "DataImpulse proxy env vars are not fully configured; running without a proxy"
            )

        yield scrapy.Request(
            url=self.WARMUP_URL,
            callback=self.parse_warmup,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def parse_warmup(self, response):
        self.logger.info("Warmup page status: %d", response.status)
        yield scrapy.Request(
            url=self.BROWSE_URL,
            callback=self.parse_location_index,
            errback=self.handle_location_index_error,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def handle_location_index_error(self, failure):
        self.logger.warning("Failed to load Zillow NJ browse index: %s", failure.value)
        yield from self._schedule_county_fallback(reason="browse_index_error")

    def parse_location_index(self, response):
        if response.status != 200:
            self.logger.warning("Zillow NJ browse index status=%s", response.status)
            yield from self._schedule_county_fallback(reason=f"browse_index_status_{response.status}")
            return

        selector = scrapy.Selector(text=self._safe_response_text(response))
        county_entries = self._extract_county_entries(selector)
        if self.max_counties:
            county_entries = county_entries[: self.max_counties]

        self.logger.info("Discovered Zillow county pages=%s", len(county_entries))
        if not county_entries:
            yield from self._schedule_county_fallback(reason="browse_index_empty")
            return

        for county_entry in county_entries:
            yield scrapy.Request(
                county_entry["url"],
                callback=self.parse_county_cities,
                errback=self.handle_county_cities_error,
                meta={
                    "county": county_entry["county"],
                    **self._proxy_meta(),
                },
                dont_filter=True,
            )

    def handle_county_cities_error(self, failure):
        request = getattr(failure, "request", None)
        county = request.meta.get("county") if request and hasattr(request, "meta") else None
        if county:
            self.logger.warning("County city index failed for %s: %s", county, failure.value)
            request = self._county_search_request(county=county)
            if request:
                yield request
            return
        self.logger.warning("County city index request failed: %s", failure.value)

    def parse_county_cities(self, response):
        county = self._clean_str(response.meta.get("county"))
        if response.status != 200:
            self.logger.warning("County city index non-200 county=%s status=%s", county, response.status)
            request = self._county_search_request(county=county)
            if request:
                yield request
            return

        selector = scrapy.Selector(text=self._safe_response_text(response))
        city_names = self._extract_city_names(selector)
        if self.max_cities:
            remaining = self.max_cities - self.city_queries_scheduled
            if remaining <= 0:
                self.logger.info("max_cities=%s reached; skipping additional cities", self.max_cities)
                return
            city_names = city_names[:remaining]

        if not city_names:
            self.logger.warning("No city names found on county page=%s; using county fallback", county)
            request = self._county_search_request(county=county)
            if request:
                yield request
            return

        self.logger.info(
            "County=%s discovered city shards=%s",
            county,
            len(city_names),
        )
        for city in city_names:
            request = self._city_search_request(city=city, county=county)
            if request:
                yield request

    def search_request(
        self,
        page,
        map_bounds,
        split_depth,
        split_path,
        strategy,
        location_label,
        users_search_term,
        county=None,
    ):
        strategy = self._normalize_strategy(strategy)
        map_bounds = self._clamp_to_nj_bounds(map_bounds)
        search_query_state = deepcopy(self.BASE_SEARCH_QUERY_STATE)
        search_query_state["pagination"]["currentPage"] = page
        search_query_state["mapBounds"] = map_bounds
        search_query_state["usersSearchTerm"] = users_search_term
        self._bump_strategy_counter("strategy_queries", strategy)

        payload = {
            "searchQueryState": search_query_state,
            "wants": self.BASE_WANTS,
            "requestId": self._request_id(page=page, split_depth=split_depth, split_path=split_path),
            "isDebugRequest": False,
        }

        referer = self.BROWSE_URL if page == 1 else f"https://www.zillow.com/nj/{page}_p/"
        headers = {"referer": referer}

        return scrapy.Request(
            url=self.SEARCH_API_URL,
            method="PUT",
            body=json.dumps(payload),
            headers=headers,
            callback=self.parse_search_results,
            errback=self.handle_search_error,
            meta={
                "page": page,
                "map_bounds": map_bounds,
                "split_depth": split_depth,
                "split_path": split_path,
                "strategy": strategy,
                "location_label": location_label,
                "users_search_term": users_search_term,
                "county": county,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def parse_search_results(self, response):
        response_text = self._safe_response_text(response)
        page = response.meta.get("page", 1)
        map_bounds = response.meta.get("map_bounds", {})
        split_depth = response.meta.get("split_depth", 0)
        split_path = response.meta.get("split_path", "root")
        strategy = response.meta.get("strategy", "bbox")
        location_label = response.meta.get("location_label", "New Jersey")
        users_search_term = response.meta.get("users_search_term", "new jersey")
        county = response.meta.get("county")
        strategy = self._normalize_strategy(strategy)

        if response.status != 200:
            if response.status in {403, 429}:
                yield from self._split_blocked_query(
                    status=response.status,
                    page=page,
                    map_bounds=map_bounds,
                    split_depth=split_depth,
                    split_path=split_path,
                    strategy=strategy,
                    location_label=location_label,
                    users_search_term=users_search_term,
                    county=county,
                    response_text=response_text,
                )
                return
            self.logger.error(
                "Zillow search request failed. strategy=%s location=%s status=%s page=%s body=%s",
                strategy,
                location_label,
                response.status,
                page,
                response_text[:500],
            )
            return

        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            self.logger.error("Failed to parse Zillow JSON page=%s: %s", page, exc)
            return

        cat1 = payload.get("cat1") or {}
        search_results = cat1.get("searchResults") or {}
        search_list = cat1.get("searchList") or {}
        list_results = search_results.get("listResults") or []
        self._bump_strategy_counter("strategy_result_pages", strategy)

        total_pages = search_list.get("totalPages") or page
        total_results = search_list.get("totalResultCount")
        results_per_page = search_list.get("resultsPerPage")
        self.logger.info(
            "Zillow strategy=%s location=%s page=%s depth=%s path=%s listings=%s total_results=%s total_pages=%s results_per_page=%s",
            strategy,
            location_label,
            page,
            split_depth,
            split_path,
            len(list_results),
            total_results,
            total_pages,
            results_per_page,
        )
        if not list_results:
            self._bump_strategy_counter("strategy_empty_pages", strategy)

        if (
            page == 1
            and self._should_split(
                total_pages=total_pages,
                total_results=total_results,
                map_bounds=map_bounds,
                split_depth=split_depth,
            )
        ):
            child_bounds = self._split_map_bounds(map_bounds)
            self.logger.info(
                "Splitting Zillow query path=%s depth=%s into %s child bounds (total_results=%s, total_pages=%s)",
                split_path,
                split_depth,
                len(child_bounds),
                total_results,
                total_pages,
            )
            for idx, bounds in enumerate(child_bounds, start=1):
                yield self.search_request(
                    page=1,
                    map_bounds=bounds,
                    split_depth=split_depth + 1,
                    split_path=f"{split_path}.{idx}",
                    strategy=strategy,
                    location_label=location_label,
                    users_search_term=users_search_term,
                    county=county,
                )
            return

        for listing in list_results:
            zpid = listing.get("zpid") or listing.get("id")
            if zpid:
                zpid = str(zpid)
                if zpid in self.seen_zpids:
                    continue
                self.seen_zpids.add(zpid)
            self._bump_strategy_counter("strategy_items", strategy)
            base_item = self.parse_listing(
                listing,
                split_depth=split_depth,
                split_path=split_path,
                strategy=strategy,
                location_label=location_label,
                county=county,
            )
            detail_request = self._detail_request(base_item)
            if isinstance(detail_request, scrapy.Request):
                yield detail_request
            else:
                self._record_contact_enrichment(detail_request)
                yield detail_request

        if list_results and page < total_pages:
            yield self.search_request(
                page=page + 1,
                map_bounds=map_bounds,
                split_depth=split_depth,
                split_path=split_path,
                strategy=strategy,
                location_label=location_label,
                users_search_term=users_search_term,
                county=county,
            )

    def handle_search_error(self, failure):
        request = getattr(failure, "request", None)
        self.logger.error(
            "Zillow search request failed for %s: %s",
            getattr(request, "url", "unknown"),
            failure.value,
        )

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except AttributeError:
            return response.body.decode("utf-8", errors="replace")

    @staticmethod
    def _absolute_url(detail_url):
        if not detail_url:
            return None
        if detail_url.startswith("http"):
            return detail_url
        return f"https://www.zillow.com{detail_url}"

    @staticmethod
    def _clean_str(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    def _should_split(self, total_pages, total_results, map_bounds, split_depth):
        if split_depth >= self.MAX_SPLIT_DEPTH:
            return False
        if not isinstance(total_pages, int):
            return False
        if total_pages <= self.MAX_SAFE_PAGE_PER_QUERY:
            return False
        if total_pages < self.MAX_PAGES_PER_QUERY and (
            not isinstance(total_results, int) or total_results <= self.MAX_RESULTS_PER_QUERY
        ):
            return False
        lat_span, lon_span = self._bounds_span(map_bounds)
        if lat_span < self.MIN_LAT_SPAN or lon_span < self.MIN_LON_SPAN:
            return False
        return True

    def _split_blocked_query(
        self,
        status,
        page,
        map_bounds,
        split_depth,
        split_path,
        strategy,
        location_label,
        users_search_term,
        county,
        response_text,
    ):
        if split_depth >= self.MAX_SPLIT_DEPTH:
            self.logger.error(
                "Zillow blocked query cannot split further. status=%s page=%s depth=%s path=%s body=%s",
                status,
                page,
                split_depth,
                split_path,
                response_text[:500],
            )
            return

        child_bounds = self._split_map_bounds(map_bounds)
        self.logger.warning(
            "Zillow blocked query status=%s strategy=%s location=%s page=%s depth=%s path=%s; splitting into %s child bounds",
            status,
            strategy,
            location_label,
            page,
            split_depth,
            split_path,
            len(child_bounds),
        )
        for idx, bounds in enumerate(child_bounds, start=1):
            yield self.search_request(
                page=1,
                map_bounds=bounds,
                split_depth=split_depth + 1,
                split_path=f"{split_path}.blocked{page}.{idx}",
                strategy=strategy,
                location_label=location_label,
                users_search_term=users_search_term,
                county=county,
            )

    @staticmethod
    def _bounds_span(map_bounds):
        north = map_bounds.get("north")
        south = map_bounds.get("south")
        east = map_bounds.get("east")
        west = map_bounds.get("west")
        if None in (north, south, east, west):
            return 0.0, 0.0
        return abs(float(north) - float(south)), abs(float(east) - float(west))

    @staticmethod
    def _split_map_bounds(map_bounds):
        north = float(map_bounds["north"])
        south = float(map_bounds["south"])
        east = float(map_bounds["east"])
        west = float(map_bounds["west"])
        mid_lat = (north + south) / 2.0
        mid_lon = (east + west) / 2.0
        return [
            {"north": north, "south": mid_lat, "east": mid_lon, "west": west},
            {"north": north, "south": mid_lat, "east": east, "west": mid_lon},
            {"north": mid_lat, "south": south, "east": mid_lon, "west": west},
            {"north": mid_lat, "south": south, "east": east, "west": mid_lon},
        ]

    @staticmethod
    def _request_id(page, split_depth, split_path):
        path_hash = sum(ord(char) for char in split_path) % 100000
        return path_hash + (split_depth * 1000) + page

    def _clamp_to_nj_bounds(self, bounds):
        nj = self.NJ_REGION_BOUNDS
        north = min(float(bounds["north"]), nj["north"])
        south = max(float(bounds["south"]), nj["south"])
        east = min(float(bounds["east"]), nj["east"])
        west = max(float(bounds["west"]), nj["west"])
        if south > north:
            south = north
        if west > east:
            west = east
        return {"north": north, "south": south, "east": east, "west": west}

    def parse_listing(self, listing, split_depth, split_path, strategy, location_label, county):
        hdp_data = listing.get("hdpData") or {}
        if not isinstance(hdp_data, dict):
            hdp_data = {}
        home_info = hdp_data.get("homeInfo") or {}
        if not isinstance(home_info, dict):
            home_info = {}

        lat_long = listing.get("latLong") or {}
        if not isinstance(lat_long, dict):
            lat_long = {}

        latitude = self._coalesce(lat_long.get("latitude"), home_info.get("latitude"))
        longitude = self._coalesce(lat_long.get("longitude"), home_info.get("longitude"))
        photo_links = self._extract_photo_links(listing, home_info)
        living_area_sqft = self._coalesce(home_info.get("livingArea"), listing.get("area"))

        broker_name = self._clean_str(listing.get("brokerName"))
        detail_url = self._absolute_url(listing.get("detailUrl"))
        return {
            "zpid": listing.get("zpid"),
            "listing_id": listing.get("id"),
            "pals_id": listing.get("palsId"),
            "url": detail_url,
            "detail_url": detail_url,
            "price": listing.get("unformattedPrice"),
            "price_display": listing.get("price"),
            "beds": self._coalesce(listing.get("beds"), home_info.get("bedrooms")),
            "baths": self._coalesce(listing.get("baths"), home_info.get("bathrooms")),
            "sqft": living_area_sqft,
            "living_area_sqft": living_area_sqft,
            "address": listing.get("address"),
            "street": listing.get("addressStreet", home_info.get("streetAddress")),
            "city": listing.get("addressCity", home_info.get("city")),
            "state": listing.get("addressState", home_info.get("state")),
            "postal_code": listing.get("addressZipcode", home_info.get("zipcode")),
            "latitude": latitude,
            "longitude": longitude,
            "status": listing.get("statusType", home_info.get("homeStatus")),
            "status_text": listing.get("statusText"),
            "broker_name": broker_name,
            "listing_office": broker_name,
            "home_type": home_info.get("homeType"),
            "days_on_zillow": self._coalesce(home_info.get("daysOnZillow"), listing.get("daysOnZillow")),
            "zestimate": listing.get("zestimate", home_info.get("zestimate")),
            "rent_zestimate": home_info.get("rentZestimate"),
            "lot_area_value": self._coalesce(home_info.get("lotAreaValue"), listing.get("lotAreaValue")),
            "lot_area_unit": self._coalesce(home_info.get("lotAreaUnit"), listing.get("lotAreaUnit")),
            "tax_assessed_value": self._coalesce(home_info.get("taxAssessedValue"), listing.get("taxAssessedValue")),
            "is_preforeclosure_auction": self._coalesce(
                home_info.get("isPreforeclosureAuction"),
                listing.get("isPreforeclosureAuction"),
            ),
            "time_on_zillow_ms": home_info.get("timeOnZillow"),
            "is_zillow_owned": listing.get("isZillowOwned", home_info.get("isZillowOwned")),
            "is_featured": listing.get("isFeaturedListing", home_info.get("isFeatured")),
            "is_showcase_listing": listing.get(
                "isShowcaseListing", home_info.get("isShowcaseListing")
            ),
            "photo_links": photo_links,
            "photos_count": len(photo_links),
            "first_photo_url": photo_links[0] if photo_links else None,
            "split_depth": split_depth,
            "split_path": split_path,
            "county": county,
            "search_strategy": strategy,
            "search_location": location_label,
            "raw_listing": listing,
        }

    def _detail_request(self, item):
        detail_url = self._clean_str((item or {}).get("url") or (item or {}).get("detail_url"))
        if not detail_url:
            return item
        return scrapy.Request(
            detail_url,
            callback=self.parse_property_detail,
            meta={"base_item": dict(item or {}), **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_property_detail(self, response):
        item = dict(response.meta.get("base_item") or {})
        item["detail_http_status"] = response.status

        if response.status != 200:
            item["detail_parse_status"] = f"non_200_{response.status}"
            self._record_contact_enrichment(item)
            yield item
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            item["detail_parse_status"] = "empty_body"
            self._record_contact_enrichment(item)
            yield item
            return

        selector = scrapy.Selector(text=response_text)
        self._enrich_contact_from_next_data(item, selector)
        self._enrich_contact_from_json_ld(item, selector)
        self._enrich_contact_from_text_fallback(item, response_text)
        item["detail_parse_status"] = "ok"
        self._record_contact_enrichment(item)
        yield item

    def _statewide_bbox_request(self, reason):
        self.logger.warning(
            "Using statewide Zillow bounding-box fallback (%s)",
            reason,
        )
        return self.search_request(
            page=1,
            map_bounds=deepcopy(self.BASE_SEARCH_QUERY_STATE["mapBounds"]),
            split_depth=0,
            split_path="bbox.root",
            strategy="bbox",
            location_label="New Jersey",
            users_search_term="new jersey",
            county=None,
        )

    def _city_search_request(self, city, county):
        city_name = self._clean_str(city)
        if not city_name:
            return None
        key = ("city", city_name.lower())
        if key in self.seen_search_keys:
            return None
        self.seen_search_keys.add(key)
        self.city_queries_scheduled += 1
        return self.search_request(
            page=1,
            map_bounds=deepcopy(self.BASE_SEARCH_QUERY_STATE["mapBounds"]),
            split_depth=0,
            split_path=f"city.{self._slug(city_name)}",
            strategy="city",
            location_label=city_name,
            users_search_term=f"{city_name}, NJ",
            county=county,
        )

    def _county_search_request(self, county):
        county_name = self._clean_str(county)
        if not county_name:
            return None
        key = ("county", county_name.lower())
        if key in self.seen_search_keys:
            return None
        self.seen_search_keys.add(key)
        self.county_queries_scheduled += 1
        return self.search_request(
            page=1,
            map_bounds=deepcopy(self.BASE_SEARCH_QUERY_STATE["mapBounds"]),
            split_depth=0,
            split_path=f"county.{self._slug(county_name)}",
            strategy="county",
            location_label=county_name,
            users_search_term=f"{county_name} County, NJ",
            county=county_name,
        )

    def _schedule_county_fallback(self, reason):
        self.logger.warning("Using county fallback (%s)", reason)
        counties = list(self.NJ_COUNTIES)
        if self.max_counties:
            counties = counties[: self.max_counties]
        scheduled = 0
        for county in counties:
            request = self._county_search_request(county=county)
            if request:
                scheduled += 1
                yield request
        if scheduled == 0:
            yield self._statewide_bbox_request(reason=f"county_fallback_empty_{reason}")

    def _extract_county_entries(self, selector):
        entries = []
        seen = set()
        for anchor in selector.xpath("//a[@href]"):
            label = self._clean_str(" ".join(anchor.xpath(".//text()").getall()))
            if not label:
                continue
            match = self.COUNTY_LABEL_PATTERN.match(label)
            if not match:
                continue
            href = self._clean_str(anchor.xpath("./@href").get())
            if not href:
                continue
            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = f"https://www.zillow.com{href}"
            else:
                continue
            if "/browse/homes/nj/" not in url or "-county" not in url.lower():
                continue
            county = self._clean_str(match.group(1))
            if not county or not url:
                continue
            if county.lower() in seen:
                continue
            seen.add(county.lower())
            entries.append({"county": county, "url": url})
        return entries

    def _extract_city_names(self, selector):
        city_names = []
        seen = set()
        for anchor in selector.xpath("//a"):
            label = self._clean_str(" ".join(anchor.xpath(".//text()").getall()))
            if not label or not label.endswith(self.CITY_LABEL_SUFFIX):
                continue
            city_name = self._clean_str(label[: -len(self.CITY_LABEL_SUFFIX)])
            if not city_name:
                continue
            lowered = city_name.lower()
            if lowered in {"about", "zestimates", "news", "research"}:
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            city_names.append(city_name)
        return city_names

    @staticmethod
    def _slug(value):
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

    @staticmethod
    def _coalesce(*values):
        for value in values:
            if value not in (None, "", []):
                return value
        return None

    def _extract_photo_links(self, listing, home_info):
        links = []
        seen = set()

        def add(url):
            value = self._clean_str(url)
            if not value:
                return
            if value.startswith("//"):
                value = f"https:{value}"
            if not value.startswith("http"):
                return
            if value in seen:
                return
            seen.add(value)
            links.append(value)

        # New Zillow shape: build URLs from baseUrl + photoKey.
        carousel = self._coalesce(
            listing.get("carouselPhotosComposable"),
            home_info.get("carouselPhotosComposable"),
        )
        if isinstance(carousel, dict):
            base_url = self._clean_str(carousel.get("baseUrl"))
            photo_data = carousel.get("photoData") or []
            if base_url and isinstance(photo_data, list):
                for photo in photo_data:
                    if not isinstance(photo, dict):
                        continue
                    photo_key = self._clean_str(photo.get("photoKey"))
                    if not photo_key:
                        continue
                    add(base_url.replace("{photoKey}", photo_key))

        # Fallbacks from legacy keys.
        for key in ("imgSrc", "hdpImageLink", "img", "thumb"):
            add(self._coalesce(listing.get(key), home_info.get(key)))
        for key in ("imgSrcs", "photos"):
            values = self._coalesce(listing.get(key), home_info.get(key))
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        add(value.get("url"))
                    else:
                        add(value)

        return links

    def _enrich_contact_from_next_data(self, item, selector):
        raw = selector.xpath("string(//script[@id='__NEXT_DATA__'])").get()
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        self._apply_contact_values_from_payload(item, payload)

    def _enrich_contact_from_json_ld(self, item, selector):
        for obj in self._extract_json_ld_objects(selector):
            if not isinstance(obj, dict):
                continue
            self._apply_contact_values_from_payload(item, obj)

    def _enrich_contact_from_text_fallback(self, item, text):
        if not item.get("listing_office_phone"):
            phone_match = self.PHONE_PATTERN.search(text or "")
            if phone_match:
                item["listing_office_phone"] = self._normalize_phone(phone_match.group(0))
        if not item.get("listing_agent_email"):
            email_match = self.EMAIL_PATTERN.search(text or "")
            if email_match:
                item["listing_agent_email"] = self._clean_str(email_match.group(0))

    def _apply_contact_values_from_payload(self, item, payload):
        if not isinstance(payload, (dict, list)):
            return

        agent_name = self._clean_str(
            self._find_first_value(
                payload,
                {"agentname", "listingagentname", "listingagent", "agent", "agentfullname"},
            )
        )
        office_name = self._clean_str(
            self._find_first_value(
                payload,
                {"brokername", "brokeragename", "listingoffice", "officename", "broker"},
            )
        )
        agent_phone = self._normalize_phone(
            self._find_first_value(
                payload,
                {"agentphonenumber", "agentphone", "listingagentphone", "mobilephonelinenumber"},
            )
        )
        office_phone = self._normalize_phone(
            self._find_first_value(
                payload,
                {"brokerphonenumber", "brokerphone", "officephone", "listingofficephone"},
            )
        )
        agent_email = self._normalize_email(
            self._find_first_value(
                payload,
                {"agentemail", "emailaddress", "listingemail", "listingagentemail"},
            )
        )
        office_email = self._normalize_email(
            self._find_first_value(
                payload,
                {"brokeremail", "officeemail", "listingofficeemail"},
            )
        )

        if not item.get("listing_agent") and agent_name:
            item["listing_agent"] = agent_name
        if not item.get("listing_office") and office_name:
            item["listing_office"] = office_name
        if not item.get("listing_agent_phone") and agent_phone:
            item["listing_agent_phone"] = agent_phone
        if not item.get("listing_office_phone"):
            item["listing_office_phone"] = office_phone or agent_phone
        if not item.get("listing_agent_email") and agent_email:
            item["listing_agent_email"] = agent_email
        if not item.get("listing_office_email") and office_email:
            item["listing_office_email"] = office_email

    @staticmethod
    def _extract_json_ld_objects(selector):
        scripts = selector.xpath('//script[@type="application/ld+json"]/text()').getall()
        for raw in scripts:
            text = (raw or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            yield from ZillowSpider._walk_json_nodes(parsed)

    @staticmethod
    def _walk_json_nodes(node):
        if isinstance(node, dict):
            yield node
            graph = node.get("@graph")
            if isinstance(graph, list):
                for child in graph:
                    yield from ZillowSpider._walk_json_nodes(child)
            return
        if isinstance(node, list):
            for child in node:
                yield from ZillowSpider._walk_json_nodes(child)

    def _normalize_phone(self, value):
        text = self._clean_str(value)
        if not text:
            return None
        match = self.PHONE_PATTERN.search(text)
        if not match:
            return None
        return self._clean_str(match.group(0))

    def _normalize_email(self, value):
        text = self._clean_str(value)
        if not text:
            return None
        match = self.EMAIL_PATTERN.search(text)
        if not match:
            return None
        return self._clean_str(match.group(0))

    def _find_first_value(self, node, candidate_keys):
        lowered = {str(key).lower() for key in (candidate_keys or set())}
        if isinstance(node, dict):
            for key, value in node.items():
                key_lower = str(key).lower()
                if key_lower in lowered and value not in (None, "", [], {}):
                    if isinstance(value, (str, int, float)):
                        return value
                nested = self._find_first_value(value, candidate_keys)
                if nested not in (None, "", [], {}):
                    return nested
        elif isinstance(node, list):
            for child in node:
                nested = self._find_first_value(child, candidate_keys)
                if nested not in (None, "", [], {}):
                    return nested
        return None

    def _record_contact_enrichment(self, item):
        self.contact_enrichment_counts["detail_records"] += 1
        if self._clean_str((item or {}).get("listing_agent")):
            self.contact_enrichment_counts["listing_agent"] += 1
        if self._clean_str((item or {}).get("listing_office_phone")):
            self.contact_enrichment_counts["listing_office_phone"] += 1
        if self._clean_str((item or {}).get("listing_agent_phone")):
            self.contact_enrichment_counts["listing_agent_phone"] += 1
        if self._clean_str((item or {}).get("listing_agent_email")):
            self.contact_enrichment_counts["listing_agent_email"] += 1
        if self._clean_str((item or {}).get("listing_office_email")):
            self.contact_enrichment_counts["listing_office_email"] += 1

    def closed(self, reason):
        summary = (
            "Zillow crawl summary reason=%s queries(city=%s county=%s bbox=%s) "
            "result_pages(city=%s county=%s bbox=%s) "
            "empty_pages(city=%s county=%s bbox=%s) "
            "items(city=%s county=%s bbox=%s total=%s) "
            "seen_zpids=%s city_queries_scheduled=%s county_queries_scheduled=%s "
            "contact_enrichment(detail_records=%s listing_agent=%s listing_office_phone=%s "
            "listing_agent_phone=%s listing_agent_email=%s listing_office_email=%s)"
        )
        total_items = sum(int(v) for v in self.strategy_items.values())
        self.logger.info(
            summary,
            reason,
            self.strategy_queries.get("city", 0),
            self.strategy_queries.get("county", 0),
            self.strategy_queries.get("bbox", 0),
            self.strategy_result_pages.get("city", 0),
            self.strategy_result_pages.get("county", 0),
            self.strategy_result_pages.get("bbox", 0),
            self.strategy_empty_pages.get("city", 0),
            self.strategy_empty_pages.get("county", 0),
            self.strategy_empty_pages.get("bbox", 0),
            self.strategy_items.get("city", 0),
            self.strategy_items.get("county", 0),
            self.strategy_items.get("bbox", 0),
            total_items,
            len(self.seen_zpids),
            self.city_queries_scheduled,
            self.county_queries_scheduled,
            self.contact_enrichment_counts.get("detail_records", 0),
            self.contact_enrichment_counts.get("listing_agent", 0),
            self.contact_enrichment_counts.get("listing_office_phone", 0),
            self.contact_enrichment_counts.get("listing_agent_phone", 0),
            self.contact_enrichment_counts.get("listing_agent_email", 0),
            self.contact_enrichment_counts.get("listing_office_email", 0),
        )
