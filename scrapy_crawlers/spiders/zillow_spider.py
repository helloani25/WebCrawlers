import json
from copy import deepcopy

import scrapy

from spiders.env_config import build_proxy_url


class ZillowSpider(scrapy.Spider):
    name = "zillow"
    allowed_domains = ["www.zillow.com"]
    handle_httpstatus_list = [400, 401, 403, 429]

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

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

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
        yield self.search_request(
            page=1,
            map_bounds=deepcopy(self.BASE_SEARCH_QUERY_STATE["mapBounds"]),
            split_depth=0,
            split_path="root",
        )

    def search_request(self, page, map_bounds, split_depth, split_path):
        map_bounds = self._clamp_to_nj_bounds(map_bounds)
        search_query_state = deepcopy(self.BASE_SEARCH_QUERY_STATE)
        search_query_state["pagination"]["currentPage"] = page
        search_query_state["mapBounds"] = map_bounds

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

        if response.status != 200:
            if response.status in {403, 429}:
                yield from self._split_blocked_query(
                    status=response.status,
                    page=page,
                    map_bounds=map_bounds,
                    split_depth=split_depth,
                    split_path=split_path,
                    response_text=response_text,
                )
                return
            self.logger.error(
                "Zillow search request failed. status=%s page=%s body=%s",
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

        total_pages = search_list.get("totalPages") or page
        total_results = search_list.get("totalResultCount")
        results_per_page = search_list.get("resultsPerPage")
        self.logger.info(
            "Zillow page=%s depth=%s path=%s listings=%s total_results=%s total_pages=%s results_per_page=%s",
            page,
            split_depth,
            split_path,
            len(list_results),
            total_results,
            total_pages,
            results_per_page,
        )

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
                )
            return

        for listing in list_results:
            zpid = listing.get("zpid") or listing.get("id")
            if zpid:
                zpid = str(zpid)
                if zpid in self.seen_zpids:
                    continue
                self.seen_zpids.add(zpid)
            yield self.parse_listing(listing, split_depth=split_depth, split_path=split_path)

        if list_results and page < total_pages:
            yield self.search_request(
                page=page + 1,
                map_bounds=map_bounds,
                split_depth=split_depth,
                split_path=split_path,
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
            "Zillow blocked query status=%s page=%s depth=%s path=%s; splitting into %s child bounds",
            status,
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

    def parse_listing(self, listing, split_depth, split_path):
        hdp_data = listing.get("hdpData") or {}
        if not isinstance(hdp_data, dict):
            hdp_data = {}
        home_info = hdp_data.get("homeInfo") or {}
        if not isinstance(home_info, dict):
            home_info = {}

        lat_long = listing.get("latLong") or {}
        if not isinstance(lat_long, dict):
            lat_long = {}

        latitude = lat_long.get("latitude", home_info.get("latitude"))
        longitude = lat_long.get("longitude", home_info.get("longitude"))

        return {
            "zpid": listing.get("zpid"),
            "listing_id": listing.get("id"),
            "pals_id": listing.get("palsId"),
            "url": self._absolute_url(listing.get("detailUrl")),
            "price": listing.get("unformattedPrice"),
            "price_display": listing.get("price"),
            "beds": listing.get("beds", home_info.get("bedrooms")),
            "baths": listing.get("baths", home_info.get("bathrooms")),
            "sqft": listing.get("area", home_info.get("livingArea")),
            "address": listing.get("address"),
            "street": listing.get("addressStreet", home_info.get("streetAddress")),
            "city": listing.get("addressCity", home_info.get("city")),
            "state": listing.get("addressState", home_info.get("state")),
            "postal_code": listing.get("addressZipcode", home_info.get("zipcode")),
            "latitude": latitude,
            "longitude": longitude,
            "status": listing.get("statusType", home_info.get("homeStatus")),
            "status_text": listing.get("statusText"),
            "broker_name": listing.get("brokerName"),
            "home_type": home_info.get("homeType"),
            "days_on_zillow": home_info.get("daysOnZillow"),
            "zestimate": listing.get("zestimate", home_info.get("zestimate")),
            "rent_zestimate": home_info.get("rentZestimate"),
            "lot_area_value": home_info.get("lotAreaValue"),
            "lot_area_unit": home_info.get("lotAreaUnit"),
            "time_on_zillow_ms": home_info.get("timeOnZillow"),
            "is_zillow_owned": listing.get("isZillowOwned", home_info.get("isZillowOwned")),
            "is_featured": listing.get("isFeaturedListing", home_info.get("isFeatured")),
            "is_showcase_listing": listing.get(
                "isShowcaseListing", home_info.get("isShowcaseListing")
            ),
            "split_depth": split_depth,
            "split_path": split_path,
            "raw_listing": listing,
        }
