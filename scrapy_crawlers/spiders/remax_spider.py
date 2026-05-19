import json
import re
from urllib.parse import quote, urlencode

import scrapy

from spiders.env_config import build_proxy_url, get_env

COLLECTION_MARKER = '{"@context":"https://schema.org","@type":"CollectionPage"'
AVAILABILITY_TO_STATUS = {
    "https://schema.org/InStock": "ACTIVE",
    "https://schema.org/SoldOut": "SOLD",
}
NUMBER_PATTERN = re.compile(r"-?[\d,.]+")
DETAIL_FIELD_ALIASES = {
    "description": {"description", "propertydescription", "publicremarks", "remarks"},
    "appliances": {"appliances", "appliance"},
    "water_heater": {"waterheater", "gaswaterheater", "electricwaterheater"},
    "bathrooms_full": {"bathroomsfull", "fullbathrooms"},
    "bathrooms_half": {"bathroomshalf", "halfbathrooms"},
    "bathrooms_total": {"bathroomstotal", "totalbathrooms", "bathrooms"},
    "bedrooms_total": {"bedroomstotal", "totalbedrooms", "bedrooms"},
    "flooring": {"flooring", "floors", "floor"},
    "interior_features": {"interiorfeatures", "interior", "rooms"},
    "living_area_sqft": {"livingarea", "livingareasqft", "livingareasquarefeet", "livingareasf"},
    "property_sub_type": {"propertysubtype", "subtype"},
    "property_type": {"propertytype", "type"},
    "year_built": {"yearbuilt", "builtin", "year"},
    "garage_spaces": {"garagespaces", "garage"},
    "lot_features": {"lotfeatures"},
    "lot_size_acres": {"lotsizeacres", "acrelot", "lotsizeacre"},
    "lot_size_sqft": {"lotsizesquarefeet", "lotsizesqft", "sqftlot"},
    "lot_size_units": {"lotsizeunits"},
    "parking_features": {"parkingfeatures"},
    "parking_total": {"parkingtotal"},
    "patio_and_porch_features": {"patioandporchfeatures"},
    "exterior_features": {"exteriorfeatures"},
    "pool_features": {"poolfeatures"},
    "zoning": {"zoning", "zone"},
    "cooling": {"cooling"},
    "heating": {"heating"},
    "city": {"city"},
    "county": {"countyorparish", "county", "parish"},
    "school_district": {"schooldistrict"},
    "subdivision_name": {"subdivisionname", "subdivision", "neighborhood"},
    "tax_annual_amount": {"taxannualamount", "annualtax", "taxes"},
    "tax_year": {"taxyear"},
    "mls_status": {"mlsstatus", "status"},
    "listing_agent": {"listingagent", "listingagentname", "listagentfullname", "agentname", "agent"},
    "listing_office": {
        "listingoffice",
        "listingofficename",
        "listofficename",
        "brokerage",
        "officename",
        "listingbrokerage",
    },
    "listing_office_phone": {"listingofficephone", "officephone", "brokeragephone"},
    "updated_at": {"updated", "lastupdated", "updatedat", "updatedon", "dateupdated", "modificationdate"},
    "days_on_website": {"daysonwebsite", "daysonmarket", "dom"},
}
MIN_REASONABLE_LIVING_AREA_SQFT = 100
MAX_REASONABLE_LIVING_AREA_SQFT = 100000


class RemaxSpider(scrapy.Spider):
    name = "remax"
    allowed_domains = ["www.remax.com"]
    handle_httpstatus_list = [401, 403, 405, 429]

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "dnt": "1",
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
        "COOKIES_ENABLED": True,
        "DOWNLOAD_DELAY": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "CURL_IMPERSONATE": "chrome110",
    }
    max_detail_retries = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {"1", "true", "yes"}
        self.proxy_disabled = disable_proxy
        self.proxy_provider = (
            kwargs.get("proxy_provider")
            or get_env("REMAX_PROXY_PROVIDER", default="dataimpulse")
        ).strip().lower()
        self.proxy_session_prefix = (
            kwargs.get("proxy_session_prefix")
            or get_env("REMAX_PROXY_SESSION_PREFIX", default="remax")
        ).strip()
        self.proxy_session_ttl = self._safe_int(
            kwargs.get("proxy_sessttl"),
            get_env("REMAX_PROXY_SESSTTL", default="25"),
            fallback=25,
        )
        self.proxy_rotate_every = self._safe_int(
            kwargs.get("proxy_rotate_every"),
            get_env("REMAX_PROXY_ROTATE_EVERY", default="20"),
            fallback=20,
        )
        self._proxy_session_seq = 0
        self._detail_request_counter = 0
        self.proxy_url = None if disable_proxy else self._build_proxy_url()
        self.state_slug = (kwargs.get("state") or get_env("REMAX_STATE", default="nj")).strip().lower()
        self.rsc_token = (kwargs.get("rsc_token") or get_env("REMAX_RSC_TOKEN", default="1")).strip()
        self.start_page = self._safe_int(
            kwargs.get("start_page"),
            get_env("REMAX_START_PAGE", default="1"),
            fallback=1,
        )
        self.max_pages = self._safe_int(
            kwargs.get("max_pages"),
            get_env("REMAX_MAX_PAGES", default="500"),
            fallback=500,
        )
        self.scrapfly_api_key = (
            kwargs.get("scrapfly_api_key")
            or get_env("SCRAPFLY_API_KEY", "SCRAPFLY_KEY")
            or ""
        ).strip()
        self.scrapfly_asp_enabled = self._is_truthy(
            kwargs.get("scrapfly_asp_enabled"),
            get_env("REMAX_SCRAPFLY_ASP_ENABLED", "SCRAPFLY_ASP_ENABLED", default="0"),
        )
        self.scrapfly_proxy_pool = (
            kwargs.get("scrapfly_proxy_pool")
            or get_env("SCRAPFLY_PROXY_POOL", default="public_residential_pool")
            or "public_residential_pool"
        ).strip()
        self.scrapfly_country = (
            kwargs.get("scrapfly_country")
            or get_env("SCRAPFLY_COUNTRY", default="us")
            or "us"
        ).strip().lower()
        self.scrapfly_render_js = self._is_truthy(
            kwargs.get("scrapfly_render_js"),
            get_env("SCRAPFLY_RENDER_JS", default="0"),
        )
        self.seen_listing_ids = set()

    def _build_proxy_url(self):
        # Scrapfly Proxy Saver has its own username/flag scheme and upstream forwarding;
        # do not append DataImpulse-style params in this mode.
        if self.proxy_provider == "scrapfly":
            return build_proxy_url()
        extra_params = []
        if self.proxy_session_prefix:
            extra_params.append(f"sessid.{self.proxy_session_prefix}-{self._proxy_session_seq}")
        if self.proxy_session_ttl and self.proxy_session_ttl > 0:
            extra_params.append(f"sessttl.{self.proxy_session_ttl}")
        return build_proxy_url(extra_params=extra_params)

    def _rotate_proxy_session(self, reason):
        if self.proxy_disabled:
            return
        self._proxy_session_seq += 1
        self.proxy_url = self._build_proxy_url()
        if self.proxy_url:
            self.logger.debug(
                "RE/MAX proxy session rotated: seq=%s reason=%s",
                self._proxy_session_seq,
                reason,
            )

    @staticmethod
    def _safe_int(value, default_value=None, fallback=None):
        candidate = value if value not in (None, "") else default_value
        try:
            return int(candidate) if candidate not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _is_truthy(value, fallback=False):
        if value in (None, ""):
            return bool(fallback)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    async def start(self):
        if self.proxy_url:
            self.logger.info("Using proxy for RE/MAX requests")
        else:
            self.logger.warning("No proxy configured; running RE/MAX spider without a proxy")
        if self.scrapfly_asp_enabled and self.scrapfly_api_key:
            self.logger.info("Scrapfly ASP fallback enabled for RE/MAX blocked detail responses")
        elif self.scrapfly_asp_enabled:
            self.logger.warning("Scrapfly ASP enabled but SCRAPFLY_API_KEY is missing; fallback disabled")
        yield self._results_request(page=self.start_page, retries=0)

    def _results_request(self, page, retries):
        url = self._build_results_url(page=page)
        return scrapy.Request(
            url,
            callback=self.parse_results_page,
            meta={
                "page": page,
                "retries": retries,
                **self._proxy_meta(),
            },
            headers=self._rsc_headers(),
            dont_filter=True,
        )

    def _build_results_url(self, page):
        search_query = {
            "sortKey": "1",
            "sortDirection": "1",
            "hasPolygon": False,
            "pageNumber": page,
            "filters": {},
        }
        encoded_search = quote(json.dumps(search_query, separators=(",", ":")))
        return (
            f"https://www.remax.com/homes-for-sale/{self.state_slug}"
            f"?searchQuery={encoded_search}&_rsc={quote(self.rsc_token or '1')}"
        )

    def _rsc_headers(self):
        router_tree = [
            "",
            {
                "children": [
                    "(rmx-design)",
                    {
                        "children": [
                            "v2",
                            {
                                "children": [
                                    "real-estate",
                                    {
                                        "children": [
                                            ["state", self.state_slug, "d"],
                                            {"children": ["__PAGE__", {}, None, "refetch"]},
                                            None,
                                            None,
                                        ]
                                    },
                                    None,
                                    None,
                                ]
                            },
                            None,
                            None,
                        ]
                    },
                    None,
                    None,
                ]
            },
            None,
            None,
        ]
        return {
            "rsc": "1",
            "next-router-state-tree": quote(json.dumps(router_tree, separators=(",", ":"))),
            "referer": f"https://www.remax.com/homes-for-sale/{self.state_slug}",
        }

    def parse_results_page(self, response):
        page = response.meta["page"]
        retries = response.meta.get("retries", 0)
        if response.status == 202 and retries < 2:
            self._rotate_proxy_session(reason=f"results_202_page_{page}")
            self.logger.info("RE/MAX returned 202 for page=%s; retrying", page)
            yield self._results_request(page=page, retries=retries + 1)
            return

        if response.status != 200:
            self.logger.warning("RE/MAX page=%s failed status=%s", page, response.status)
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.warning("RE/MAX page=%s empty response", page)
            return

        raw_items = self._extract_item_list(response_text)
        yielded_count = 0
        for raw_item in raw_items:
            listing = self._parse_listing(raw_item, page=page)
            if not listing:
                continue
            dedupe_id = listing.get("mls_id") or listing.get("detail_url")
            if not dedupe_id or dedupe_id in self.seen_listing_ids:
                continue
            self.seen_listing_ids.add(dedupe_id)
            yielded_count += 1
            detail_request = self._detail_request(listing)
            if isinstance(detail_request, scrapy.Request):
                yield detail_request
            else:
                yield self._as_property_item(detail_request)

        self.logger.info(
            "RE/MAX state=%s page=%s raw_items=%s yielded_new=%s",
            self.state_slug,
            page,
            len(raw_items),
            yielded_count,
        )

        # Stop when page has no usable new listings, otherwise paginate forward.
        if raw_items and yielded_count > 0 and page < self.max_pages:
            yield self._results_request(page=page + 1, retries=0)

    def _detail_request(self, item):
        detail_url = item.get("detail_url")
        if not detail_url:
            return item
        self._detail_request_counter += 1
        if self.proxy_rotate_every and self.proxy_rotate_every > 0:
            if self._detail_request_counter % self.proxy_rotate_every == 0:
                self._rotate_proxy_session(reason=f"detail_request_count_{self._detail_request_counter}")
        return scrapy.Request(
            detail_url,
            callback=self.parse_detail_page,
            meta={"base_item": item, "detail_retries": 0, **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_detail_page(self, response):
        item = dict(response.meta.get("base_item") or {})
        detail_retries = int(response.meta.get("detail_retries", 0) or 0)

        if response.status == 202 and detail_retries < self.max_detail_retries:
            self._rotate_proxy_session(reason=f"detail_202_{item.get('mls_id')}_{detail_retries}")
            retry_url = self._next_detail_retry_url(
                current_url=response.url,
                retry_count=detail_retries,
                original_url=item.get("detail_url"),
            )
            self.logger.debug(
                "RE/MAX detail returned 202; retrying listing=%s retry=%s url=%s -> %s",
                item.get("mls_id"),
                detail_retries + 1,
                response.url,
                retry_url,
            )
            yield scrapy.Request(
                retry_url,
                callback=self.parse_detail_page,
                meta={
                    "base_item": item,
                    "detail_retries": detail_retries + 1,
                    **self._proxy_meta(),
                },
                dont_filter=True,
            )
            return

        if response.status == 202:
            fallback_request = self._scrapfly_detail_request(
                item=item,
                original_url=item.get("detail_url") or response.url,
                blocked_status=202,
            )
            if isinstance(fallback_request, scrapy.Request):
                yield fallback_request
            else:
                item["detail_http_status"] = 202
                item["detail_parse_status"] = "blocked_202_after_retries"
                yield self._as_property_item(item)
            return

        if response.status != 200:
            fallback_request = self._scrapfly_detail_request(
                item=item,
                original_url=item.get("detail_url") or response.url,
                blocked_status=response.status,
            )
            if isinstance(fallback_request, scrapy.Request):
                yield fallback_request
            else:
                item["detail_http_status"] = response.status
                item["detail_parse_status"] = f"non_200_{response.status}"
                yield self._as_property_item(item)
            return

        item["detail_http_status"] = response.status
        item["detail_parse_status"] = "ok"
        response_text = self._safe_response_text(response)
        if not response_text:
            item["detail_parse_status"] = "empty_body"
            yield self._as_property_item(item)
            return

        selector = scrapy.Selector(text=response_text)
        detail_fields = self._extract_detail_fields(selector)
        detail_fields.update(self._extract_detail_fields_from_serialized_text(response_text))
        detail_fields.update(self._extract_detail_text_fallbacks(selector))
        self._apply_detail_fields(item, detail_fields)
        yield self._as_property_item(item)

    def parse_detail_page_scrapfly(self, response):
        item = dict(response.meta.get("base_item") or {})
        blocked_status = response.meta.get("blocked_status")
        upstream_status, upstream_text = self._extract_scrapfly_upstream_result(response)
        if upstream_status is None:
            upstream_status = blocked_status or response.status

        item["detail_http_status"] = upstream_status
        if upstream_status != 200:
            item["detail_parse_status"] = f"scrapfly_non_200_{upstream_status}"
            yield self._as_property_item(item)
            return

        if not upstream_text:
            item["detail_parse_status"] = "scrapfly_empty_body"
            yield self._as_property_item(item)
            return

        selector = scrapy.Selector(text=upstream_text)
        detail_fields = self._extract_detail_fields(selector)
        detail_fields.update(self._extract_detail_fields_from_serialized_text(upstream_text))
        detail_fields.update(self._extract_detail_text_fallbacks(selector))
        self._apply_detail_fields(item, detail_fields)
        item["detail_parse_status"] = "scrapfly_ok"
        yield self._as_property_item(item)

    @staticmethod
    def _as_property_item(document):
        return dict(document or {})

    def _scrapfly_detail_request(self, item, original_url, blocked_status):
        if not (self.scrapfly_asp_enabled and self.scrapfly_api_key and original_url):
            return item
        params = {
            "key": self.scrapfly_api_key,
            "url": original_url,
            "asp": "true",
            "country": self.scrapfly_country,
            "proxy_pool": self.scrapfly_proxy_pool,
        }
        if self.scrapfly_render_js:
            params["render_js"] = "true"
        scrapfly_url = f"https://api.scrapfly.io/scrape?{urlencode(params)}"
        self.logger.info(
            "RE/MAX detail blocked status=%s mls_id=%s; retrying via Scrapfly ASP",
            blocked_status,
            item.get("mls_id"),
        )
        self.logger.debug(
            "RE/MAX detail retrying with Scrapfly ASP: status=%s mls_id=%s url=%s",
            blocked_status,
            item.get("mls_id"),
            original_url,
        )
        return scrapy.Request(
            scrapfly_url,
            callback=self.parse_detail_page_scrapfly,
            headers={"accept": "application/json"},
            meta={
                "base_item": dict(item or {}),
                "blocked_status": blocked_status,
            },
            dont_filter=True,
        )

    @staticmethod
    def _extract_scrapfly_upstream_result(response):
        text = RemaxSpider._safe_response_text(response)
        if not text:
            return None, ""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, text
        result = payload.get("result") or {}
        status = result.get("status_code")
        content = result.get("content")
        if isinstance(content, str):
            return status, content
        return status, ""

    @staticmethod
    def _next_detail_retry_url(current_url, retry_count, original_url=None):
        def to_luxury(value):
            text = str(value or "").strip()
            if not text:
                return ""
            if "://www.remax.com/luxury/" in text or "://remax.com/luxury/" in text:
                return text
            if "://www.remax.com/" in text:
                return text.replace("://www.remax.com/", "://www.remax.com/luxury/", 1)
            if "://remax.com/" in text:
                return text.replace("://remax.com/", "://remax.com/luxury/", 1)
            return text

        def to_standard(value):
            text = str(value or "").strip()
            if not text:
                return ""
            return text.replace("://www.remax.com/luxury/", "://www.remax.com/", 1).replace(
                "://remax.com/luxury/",
                "://remax.com/",
                1,
            )

        def with_retry_param(value, num):
            text = str(value or "").strip()
            if not text:
                return ""
            separator = "&" if "?" in text else "?"
            return f"{text}{separator}remax_retry={num}"

        candidates = []
        for base in (original_url, current_url):
            base = str(base or "").strip()
            if not base:
                continue
            candidates.append(base)
            candidates.append(to_luxury(base))
            candidates.append(to_standard(base))

        deduped = []
        seen = set()
        for candidate in candidates:
            if not candidate:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            deduped.append(candidate)

        current = str(current_url or "").strip()
        ordered = [c for c in deduped if c != current]
        ordered.extend(with_retry_param(c, retry_count + 1) for c in ordered[:2])
        if not ordered:
            return current_url
        return ordered[min(retry_count, len(ordered) - 1)]

    def _extract_item_list(self, response_text):
        items = self._extract_item_list_from_html_json_ld(response_text)
        if items:
            return items
        collection_json = self._extract_collection_page_json(response_text)
        if not collection_json:
            return []
        return (
            collection_json.get("mainEntity", {})
            .get("itemListElement", [])
        )

    def _extract_item_list_from_html_json_ld(self, response_text):
        selector = scrapy.Selector(text=response_text)
        for script_text in selector.xpath('//script[@type="application/ld+json"]/text()').getall():
            payload = (script_text or "").strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(data, list):
                for entry in data:
                    items = self._items_from_ld_entry(entry)
                    if items:
                        return items
                continue
            items = self._items_from_ld_entry(data)
            if items:
                return items
        return []

    @staticmethod
    def _items_from_ld_entry(entry):
        if not isinstance(entry, dict):
            return []
        if entry.get("@type") != "CollectionPage":
            return []
        return entry.get("mainEntity", {}).get("itemListElement", []) or []

    def _extract_collection_page_json(self, response_text):
        start = response_text.find(COLLECTION_MARKER)
        if start < 0:
            return None
        candidate = self._slice_json_object(response_text, start)
        if not candidate:
            return None
        try:
            return json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _slice_json_object(text, start_idx):
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start_idx, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
                continue
            if char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx: idx + 1]
        return None

    def _parse_listing(self, raw_item, page):
        item = (raw_item or {}).get("item", {})
        if not item:
            return None
        offer = item.get("offers", {}) or {}
        offered_home = offer.get("itemOffered", {}) or {}
        address = offered_home.get("address", {}) or {}
        identifier = item.get("identifier", {}) or {}

        detail_url = item.get("url") or item.get("@id")
        mls_id = identifier.get("value")
        status = AVAILABILITY_TO_STATUS.get(offer.get("availability"), offer.get("availability"))

        return {
            "source": "remax",
            "mls_id": str(mls_id).strip() if mls_id not in (None, "") else None,
            "detail_url": detail_url,
            "address": address.get("streetAddress"),
            "city": address.get("addressLocality"),
            "state": address.get("addressRegion"),
            "postal_code": address.get("postalCode"),
            "county": None,
            "list_price": self._to_int(offer.get("price")),
            "beds": self._to_int(offered_home.get("numberOfBedrooms")),
            "baths": self._to_float(offered_home.get("numberOfBathroomsTotal")),
            "sqft": self._to_int((offered_home.get("floorSize") or {}).get("value")),
            "property_type": offered_home.get("@type"),
            "status": status,
            "description": None,
            "appliances": None,
            "water_heater": None,
            "bathrooms_full": None,
            "bathrooms_half": None,
            "bathrooms_total": self._to_float(offered_home.get("numberOfBathroomsTotal")),
            "bedrooms_total": self._to_int(offered_home.get("numberOfBedrooms")),
            "flooring": None,
            "interior_features": None,
            "living_area_sqft": self._to_int((offered_home.get("floorSize") or {}).get("value")),
            "property_sub_type": None,
            "year_built": self._to_int(offered_home.get("yearBuilt")),
            "garage_spaces": None,
            "lot_features": None,
            "lot_size_acres": None,
            "lot_size_sqft": None,
            "lot_size_units": None,
            "parking_features": None,
            "parking_total": None,
            "patio_and_porch_features": None,
            "exterior_features": None,
            "pool_features": None,
            "zoning": None,
            "cooling": None,
            "heating": None,
            "school_district": None,
            "subdivision_name": None,
            "tax_annual_amount": None,
            "tax_year": None,
            "mls_status": None,
            "listing_agent": None,
            "listing_office": None,
            "listing_office_phone": None,
            "updated_at": None,
            "days_on_website": None,
            "detail_http_status": None,
            "detail_parse_status": None,
            "page": page,
        }

    def _extract_detail_fields(self, selector):
        values = {}
        for payload in self._iter_script_json_payloads(selector):
            self._collect_detail_fields(payload, values)
        return values

    def _extract_detail_fields_from_serialized_text(self, response_text):
        values = {}
        if not response_text:
            return values

        normalized = response_text.replace('\\"', '"')

        title_id_data_pattern = re.compile(
            r'\{"title":"(?P<title>[^"]+)","id":"(?P<id>[^"]+)","data":"(?P<data>.*?)"\}',
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in title_id_data_pattern.finditer(normalized):
            title = self._decode_escaped_text(match.group("title"))
            key_id = self._decode_escaped_text(match.group("id"))
            data = self._decode_escaped_text(match.group("data"))
            field = self._map_detail_field(key_id) or self._map_detail_field(title)
            if not field or values.get(field) not in (None, ""):
                continue
            parsed = self._coerce_field_value(field, data)
            if parsed not in (None, ""):
                values[field] = parsed

        name_value_pattern = re.compile(
            r'\{"value":"(?P<value>.*?)","name":"(?P<name>[^"]+)"\}',
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in name_value_pattern.finditer(normalized):
            name = self._decode_escaped_text(match.group("name"))
            data = self._decode_escaped_text(match.group("value"))
            field = self._map_detail_field(name)
            if not field or values.get(field) not in (None, ""):
                continue
            parsed = self._coerce_field_value(field, data)
            if parsed not in (None, ""):
                values[field] = parsed
        return values

    def _iter_script_json_payloads(self, selector):
        for script_text in selector.xpath("//script/text()").getall():
            text = (script_text or "").strip()
            if not text or text[0] not in "{[":
                continue
            if len(text) > 4_000_000:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            yield data

    def _collect_detail_fields(self, node, out):
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                # Handle label/value structures used across schema and app payloads.
                label = (
                    current.get("name")
                    or current.get("label")
                    or current.get("key")
                    or current.get("title")
                    or current.get("id")
                )
                field = self._map_detail_field(label) if isinstance(label, str) else None
                if field:
                    value_candidate = None
                    for candidate_key in ("value", "data", "text", "name"):
                        if candidate_key in current and current.get(candidate_key) not in (None, "", [], {}):
                            value_candidate = current.get(candidate_key)
                            break
                    if value_candidate is not None:
                        self._set_field_value(out, field, value_candidate)

                # Backwards-compatible name/value handling.
                if isinstance(label, str) and "value" in current:
                    field = self._map_detail_field(label)
                    if field:
                        self._set_field_value(out, field, current.get("value"))

                for key, value in current.items():
                    field = self._map_detail_field(key)
                    if field:
                        self._set_field_value(out, field, value)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
                continue
            if isinstance(current, list):
                for value in current:
                    if isinstance(value, (dict, list)):
                        stack.append(value)

    @staticmethod
    def _normalize_field_key(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _map_detail_field(self, key):
        normalized = self._normalize_field_key(key)
        if not normalized:
            return None
        for field, aliases in DETAIL_FIELD_ALIASES.items():
            if normalized in aliases:
                return field
        return None

    def _set_field_value(self, out, field, raw_value):
        if raw_value in (None, "", [], {}):
            return
        parsed = self._coerce_field_value(field, raw_value)
        if parsed in (None, ""):
            return
        if out.get(field) in (None, ""):
            out[field] = parsed

    def _coerce_field_value(self, field, value):
        if isinstance(value, list):
            parts = []
            for entry in value:
                if isinstance(entry, (str, int, float)):
                    part = str(entry).strip()
                    if part:
                        parts.append(part)
            if not parts:
                return None
            return "\n".join(parts)

        if isinstance(value, dict):
            if "value" in value:
                return self._coerce_field_value(field, value.get("value"))
            if "name" in value:
                return self._coerce_field_value(field, value.get("name"))
            return None

        if field in {"bathrooms_full", "bathrooms_half", "bedrooms_total", "year_built", "garage_spaces", "lot_size_sqft", "parking_total", "tax_year", "living_area_sqft", "days_on_website"}:
            return self._to_int(value)
        if field in {"bathrooms_total", "lot_size_acres"}:
            return self._to_float(value)
        if field == "tax_annual_amount":
            return self._to_int(value)
        text_value = str(value).strip()
        if field in {"updated_at", "listing_agent", "listing_office", "listing_office_phone"} and len(text_value) > 240:
            return None
        if field in {
            "appliances",
            "interior_features",
            "parking_features",
            "lot_features",
            "patio_and_porch_features",
            "exterior_features",
        } and len(text_value) > 2000:
            return None
        return text_value

    def _apply_detail_fields(self, item, values):
        for field, value in values.items():
            if field == "mls_status":
                if item.get("mls_status") in (None, ""):
                    item["mls_status"] = value
                if item.get("status") in (None, ""):
                    item["status"] = value
                continue
            if item.get(field) in (None, ""):
                item[field] = value

        office = self._clean_str(item.get("listing_office"))
        office_phone = self._clean_str(item.get("listing_office_phone"))
        if office and "," in office and not office_phone:
            split_name, split_phone = self._split_office_and_phone(office)
            office = split_name or office
            office_phone = split_phone or office_phone
        if office and office_phone and office_phone in office:
            office = office.replace(office_phone, "").rstrip(" ,")
        if office:
            item["listing_office"] = office
        if office_phone:
            item["listing_office_phone"] = office_phone

        if item.get("beds") is None and item.get("bedrooms_total") is not None:
            item["beds"] = item.get("bedrooms_total")
        if item.get("baths") is None and item.get("bathrooms_total") is not None:
            item["baths"] = item.get("bathrooms_total")
        if item.get("bathrooms_total") is None and item.get("bathrooms_full") is not None:
            item["bathrooms_total"] = float(item.get("bathrooms_full"))
        if item.get("baths") is None and item.get("bathrooms_full") is not None:
            item["baths"] = float(item.get("bathrooms_full"))
        if item.get("sqft") is None and item.get("living_area_sqft") is not None:
            item["sqft"] = item.get("living_area_sqft")

        living_area = item.get("living_area_sqft")
        sqft = item.get("sqft")
        living_area_ok = self._is_reasonable_living_area_sqft(living_area)
        sqft_ok = self._is_reasonable_living_area_sqft(sqft)

        if not living_area_ok:
            item["living_area_sqft"] = None
        if not sqft_ok:
            item["sqft"] = None

        # Prefer a valid living area when sqft is missing/invalid.
        if item.get("sqft") is None and living_area_ok:
            item["sqft"] = living_area
        if item.get("living_area_sqft") is None and sqft_ok:
            item["living_area_sqft"] = sqft

        if item.get("bathrooms_total") is not None and not (0 < float(item["bathrooms_total"]) < 30):
            item["bathrooms_total"] = None
            if item.get("baths") is not None and not (0 < float(item["baths"]) < 30):
                item["baths"] = None
        if isinstance(item.get("updated_at"), str) and len(item.get("updated_at")) > 240:
            item["updated_at"] = None
        if isinstance(item.get("appliances"), str):
            app_lower = item["appliances"].lower()
            if '\\"' in item["appliances"] or "property search" in app_lower or len(item["appliances"]) > 2000:
                item["appliances"] = None
        if isinstance(item.get("flooring"), str):
            item["flooring"] = self._clean_multivalue_field(
                item.get("flooring"),
                drop_tokens={"see remarks", "other see remarks", "other-see remarks"},
            )
        if item.get("county"):
            item["county"] = self._clean_county_text(item.get("county"))

    def _extract_detail_text_fallbacks(self, selector):
        # Prefer scoped listing-details text; whole-page text includes comparable listings.
        detail_flat = self._extract_scoped_detail_text(selector)

        # Keep a whole-page view only for description/meta fallback.
        page_text_nodes = selector.xpath(
            "//body//*[not(self::script) and not(self::style) and not(self::noscript)]/text()[normalize-space()]"
        ).getall()
        if not page_text_nodes:
            return {}
        page_flat = " ".join(part.strip() for part in page_text_nodes if part and part.strip())
        if not page_flat:
            return {}
        flat = detail_flat or page_flat

        values = {}
        # Prefer DOM label/value extraction when available on fully rendered detail pages.
        self._extract_detail_fields_from_dom(selector, values)
        boundary_labels = (
            "INTERIOR",
            "BUILDING AND CONSTRUCTION",
            "EXTERIOR AND LOT",
            "UTILITIES",
            "AREA AND SCHOOLS",
            "FINANCIAL INFO",
            "ADDITIONAL INFO",
            "Tax Annual Amount",
            "Tax Year",
            "Lot Size Square Feet",
            "Lot Size Acres",
            "Lot Size Units",
            "County Or Parish",
            "School District",
            "Subdivision Name",
            "Mls Status",
            "Listing Agent",
            "Listing Office",
            "Updated",
            "Property Description",
            "Bedrooms Total",
            "Bathrooms Total",
            "Property Type",
            "Property Sub Type",
            "Year Built",
            "Zoning",
            "Heating",
            "Cooling",
            "Parking Total",
            "Appliances",
            "Bathrooms Full",
            "Bathrooms Half",
            "Patio And Porch Features",
            "Exterior Features",
            "Garage Spaces",
            "Lot Features",
            "Parking Features",
            "Pool Features",
            "Beds",
            "Baths",
            "Sq Ft",
            "Living Area",
            "Days on website",
        )
        desc_match = re.search(
            r"Property description for .*?\s+(?P<desc>.*?)(?=\s+Listing Agent\b|\s+Listing Office\b|\s+Updated\b|\s+Interior Features\b|\s+Property Type\b|\s+Beds\b|\s+Baths\b|\s+Sq Ft\b|$)",
            page_flat,
            flags=re.IGNORECASE,
        )
        if desc_match:
            desc = self._clean_description_text(desc_match.group("desc"))
            if desc:
                values["description"] = desc

        # Meta description is typically cleaner than flattened text.
        meta_desc = selector.xpath("normalize-space(//meta[@property='og:description']/@content)").get()
        if not meta_desc:
            meta_desc = selector.xpath("normalize-space(//meta[@name='description']/@content)").get()
        meta_desc = self._clean_description_text(meta_desc)
        if meta_desc and values.get("description") in (None, ""):
            values["description"] = meta_desc

        agent_value = self._extract_labeled_value(
            flat,
            "Listing Agent",
            next_labels=boundary_labels,
        )
        if agent_value:
            if len(agent_value) <= 120:
                values["listing_agent"] = agent_value

        office_value = self._extract_labeled_value(
            flat,
            "Listing Office",
            next_labels=boundary_labels,
        )
        if office_value:
            office_name, office_phone = self._split_office_and_phone(office_value)
            office_name = office_name or office_value
            if office_name and len(office_name) <= 180:
                values["listing_office"] = office_name
            if office_phone and len(office_phone) <= 40:
                values["listing_office_phone"] = office_phone

        updated_value = self._extract_labeled_value(
            flat,
            "Updated",
            next_labels=boundary_labels,
        )
        if updated_value and self._looks_like_updated_text(updated_value):
            values["updated_at"] = updated_value

        tax_annual_value = self._extract_labeled_value(
            flat,
            "Tax Annual Amount",
            next_labels=boundary_labels,
        )
        if tax_annual_value:
            parsed_tax = self._to_int(tax_annual_value)
            if parsed_tax is not None:
                values["tax_annual_amount"] = parsed_tax

        tax_year_value = self._extract_labeled_value(
            flat,
            "Tax Year",
            next_labels=boundary_labels,
        )
        if tax_year_value:
            parsed_tax_year = self._to_int(tax_year_value)
            if parsed_tax_year is not None:
                values["tax_year"] = parsed_tax_year

        lot_size_sqft_value = self._extract_labeled_value(
            flat,
            "Lot Size Square Feet",
            next_labels=boundary_labels,
        )
        if lot_size_sqft_value:
            parsed_lot_sqft = self._to_int(lot_size_sqft_value)
            if parsed_lot_sqft is not None:
                values["lot_size_sqft"] = parsed_lot_sqft

        lot_size_acres_value = self._extract_labeled_value(
            flat,
            "Lot Size Acres",
            next_labels=boundary_labels,
        )
        if lot_size_acres_value:
            parsed_lot_acres = self._to_float(lot_size_acres_value)
            if parsed_lot_acres is not None:
                values["lot_size_acres"] = parsed_lot_acres

        if values.get("lot_size_acres") is None and values.get("lot_size_sqft") is not None:
            values["lot_size_acres"] = round(float(values["lot_size_sqft"]) / 43560.0, 4)

        county_value = self._extract_labeled_value(
            flat,
            "County Or Parish",
            next_labels=boundary_labels,
        )
        if county_value:
            cleaned_county = self._clean_county_text(county_value)
            if cleaned_county:
                values["county"] = cleaned_county

        appliances_value = self._extract_labeled_value(
            flat,
            "Appliances",
            next_labels=boundary_labels,
        )
        if appliances_value and self._looks_like_appliances_text(appliances_value):
            values["appliances"] = appliances_value

        bathrooms_full_value = self._extract_labeled_value(
            flat,
            "Bathrooms Full",
            next_labels=boundary_labels,
        )
        if bathrooms_full_value:
            parsed_bathrooms_full = self._to_int(bathrooms_full_value)
            if parsed_bathrooms_full is not None:
                values["bathrooms_full"] = parsed_bathrooms_full

        bathrooms_half_value = self._extract_labeled_value(
            flat,
            "Bathrooms Half",
            next_labels=boundary_labels,
        )
        if bathrooms_half_value:
            parsed_bathrooms_half = self._to_int(bathrooms_half_value)
            if parsed_bathrooms_half is not None:
                values["bathrooms_half"] = parsed_bathrooms_half

        beds_value = self._extract_labeled_value(
            flat,
            "Beds",
            next_labels=boundary_labels,
        )
        if beds_value and values.get("bedrooms_total") is None and self._is_compact_numeric_fallback(beds_value):
            parsed_beds = self._to_int(beds_value)
            if parsed_beds is not None:
                values["bedrooms_total"] = parsed_beds

        baths_value = self._extract_labeled_value(
            flat,
            "Baths",
            next_labels=boundary_labels,
        )
        if baths_value and values.get("bathrooms_total") is None and self._is_compact_numeric_fallback(baths_value):
            parsed_baths = self._to_float(baths_value)
            if parsed_baths is not None:
                values["bathrooms_total"] = parsed_baths

        living_area_value = self._extract_labeled_value(
            flat,
            "Living Area",
            next_labels=boundary_labels,
        )
        if living_area_value:
            parsed_living_area = self._to_int(living_area_value)
            if parsed_living_area is not None:
                values["living_area_sqft"] = parsed_living_area

        parking_total_value = self._extract_labeled_value(
            flat,
            "Parking Total",
            next_labels=boundary_labels,
        )
        if parking_total_value:
            parsed_parking_total = self._to_int(parking_total_value)
            if parsed_parking_total is not None:
                values["parking_total"] = parsed_parking_total
        if values.get("parking_total") is None:
            parking_spaces_match = re.search(r"\b(\d+)\s+parking\s+spaces?\b", flat, flags=re.IGNORECASE)
            if parking_spaces_match:
                values["parking_total"] = self._to_int(parking_spaces_match.group(1))

        patio_porch_value = self._extract_labeled_value(
            flat,
            "Patio And Porch Features",
            next_labels=boundary_labels,
        )
        if patio_porch_value:
            values["patio_and_porch_features"] = patio_porch_value

        exterior_features_value = self._extract_labeled_value(
            flat,
            "Exterior Features",
            next_labels=boundary_labels,
        )
        if exterior_features_value:
            values["exterior_features"] = exterior_features_value

        days_value = self._extract_labeled_value(
            flat,
            "Days on website",
            next_labels=boundary_labels,
        )
        if days_value:
            parsed_days = self._to_int(days_value)
            if parsed_days is not None:
                values["days_on_website"] = parsed_days
        if values.get("days_on_website") is None:
            days_match = re.search(r"\b(\d+)\s+Days?\s+on\s+website\b", flat, flags=re.IGNORECASE)
            if days_match:
                values["days_on_website"] = self._to_int(days_match.group(1))

        return values

    @staticmethod
    def _extract_scoped_detail_text(selector):
        # Scope fallback parsing to listing detail sections to avoid "Comparable Listings" contamination.
        scoped_nodes = selector.xpath(
            "//*[contains(@class, 'd-listing-features') "
            "or contains(@class, 'd-listing-details') "
            "or @data-testid='d-listing-features-extended' "
            "or @data-testid='d-listing-features-container' "
            "or @data-testid='d-listing-features-main']"
            "//text()[normalize-space()]"
        ).getall()
        if not scoped_nodes:
            return ""
        return " ".join(part.strip() for part in scoped_nodes if part and part.strip())

    @staticmethod
    def _is_compact_numeric_fallback(value):
        text = str(value or "").strip()
        if not text:
            return False
        if len(text) > 32:
            return False
        lower = text.lower()
        if "listing by" in lower or "mls" in lower or "get real estate advice" in lower:
            return False
        # keep only simple numeric-style strings
        return bool(re.fullmatch(r"[\d,]+(?:\.\d+)?", text))

    @staticmethod
    def _split_office_and_phone(value):
        text = str(value or "").strip()
        if not text:
            return None, None
        if "," not in text:
            return text, None
        left, right = text.rsplit(",", 1)
        phone = right.strip()
        if re.search(r"\d", phone):
            return left.strip(), phone
        return text, None

    @staticmethod
    def _decode_escaped_text(value):
        text = str(value or "")
        if not text:
            return ""
        text = text.replace("\\/", "/")
        text = text.replace("\\u0026", "&")
        try:
            text = bytes(text, "utf-8").decode("unicode_escape")
        except Exception:
            pass
        return text.strip()

    @staticmethod
    def _looks_like_updated_text(value):
        text = str(value or "").strip()
        if not text:
            return False
        if len(text) > 160:
            return False
        patterns = (
            r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b",
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
            r"\b20\d{2}\b",
        )
        lower = text.lower()
        return sum(1 for p in patterns if re.search(p, lower, flags=re.IGNORECASE)) >= 2

    @staticmethod
    def _looks_like_appliances_text(value):
        text = str(value or "").strip()
        if not text:
            return False
        if len(text) > 600:
            return False
        lower = text.lower()
        if "property search" in lower or "all rights reserved" in lower:
            return False
        appliance_tokens = (
            "oven",
            "range",
            "dishwasher",
            "dryer",
            "microwave",
            "refrigerator",
            "washer",
            "water heater",
        )
        return any(token in lower for token in appliance_tokens)

    @staticmethod
    def _clean_county_text(value):
        text = str(value or "").strip()
        if not text:
            return None
        headings = (
            "FINANCIAL INFO",
            "ADDITIONAL INFO",
            "AREA AND SCHOOLS",
            "UTILITIES",
            "EXTERIOR AND LOT",
            "BUILDING AND CONSTRUCTION",
            "INTERIOR",
        )
        upper = text.upper()
        for heading in headings:
            marker = f" {heading}"
            idx = upper.find(marker)
            if idx > 0:
                text = text[:idx].strip(" -:\t\r\n")
                break
        return text or None

    @staticmethod
    def _clean_multivalue_field(value, drop_tokens=None):
        text = str(value or "").strip()
        if not text:
            return None
        tokens = {
            re.sub(r"[^a-z]+", " ", str(t or "").lower()).strip()
            for t in (drop_tokens or set())
            if str(t or "").strip()
        }
        parts = re.split(r"[\n,;]+", text)
        cleaned = []
        for raw in parts:
            part = str(raw or "").strip(" \t\r\n,;")
            if not part:
                continue
            normalized = re.sub(r"[^a-z]+", " ", part.lower()).strip()
            if normalized and normalized in tokens:
                continue
            cleaned.append(part)
        if not cleaned:
            return None
        # Deduplicate while preserving order.
        unique = list(dict.fromkeys(cleaned))
        separator = "\n" if "\n" in text and "," not in text else ", "
        return separator.join(unique)

    @staticmethod
    def _is_reasonable_living_area_sqft(value):
        if value is None:
            return False
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        return MIN_REASONABLE_LIVING_AREA_SQFT <= number <= MAX_REASONABLE_LIVING_AREA_SQFT

    def _extract_detail_fields_from_dom(self, selector, out):
        label_to_field = {
            "Listing Agent": "listing_agent",
            "Listing Office": "listing_office",
            "Updated": "updated_at",
            "Tax Annual Amount": "tax_annual_amount",
            "Tax Year": "tax_year",
            "Lot Size Square Feet": "lot_size_sqft",
            "Lot Size Acres": "lot_size_acres",
            "Lot Size Units": "lot_size_units",
            "County Or Parish": "county",
            "School District": "school_district",
            "Subdivision Name": "subdivision_name",
            "Appliances": "appliances",
            "Bathrooms Full": "bathrooms_full",
            "Bathrooms Half": "bathrooms_half",
            "Bathrooms Total": "bathrooms_total",
            "Bedrooms Total": "bedrooms_total",
            "Living Area": "living_area_sqft",
            "Parking Total": "parking_total",
            "Mls Status": "mls_status",
            "Year Built": "year_built",
            "Property Type": "property_type",
            "Property Sub Type": "property_sub_type",
            "Heating": "heating",
            "Cooling": "cooling",
            "Zoning": "zoning",
            "Pool Features": "pool_features",
            "Parking Features": "parking_features",
            "Patio And Porch Features": "patio_and_porch_features",
            "Exterior Features": "exterior_features",
            "Garage Spaces": "garage_spaces",
            "Interior Features": "interior_features",
            "Flooring": "flooring",
            "Water Heater": "water_heater",
        }

        for label, field in label_to_field.items():
            raw = self._extract_dom_label_value(selector, label)
            if raw in (None, ""):
                continue
            parsed = self._coerce_field_value(field, raw)
            if parsed in (None, ""):
                continue
            if out.get(field) in (None, ""):
                out[field] = parsed

    def _extract_dom_label_value(self, selector, label):
        # Typical structure: <span>Label</span><p>Value</p> or <span>Label</span><ul><li>..</li></ul>
        p_val = selector.xpath(
            f"normalize-space((//span[normalize-space()={json.dumps(label)}]/following-sibling::p[1])[1])"
        ).get()
        if p_val:
            return p_val

        li_vals = selector.xpath(
            f"(//span[normalize-space()={json.dumps(label)}]/following-sibling::ul[1]/li//text())[normalize-space()]"
        ).getall()
        if li_vals:
            parts = [self._clean_str(v) for v in li_vals]
            parts = [v for v in parts if v]
            if parts:
                return "\n".join(parts)

        # Some labels are inline in the same parent element.
        inline = selector.xpath(
            f"normalize-space((//span[normalize-space()={json.dumps(label)}]/parent::*[1])[1])"
        ).get()
        if inline:
            cleaned = inline.replace(label, "", 1).strip(" :\u00a0")
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def _extract_labeled_value(flat_text, label, next_labels=()):
        escaped_label = re.escape(label)
        lookahead_parts = [r"$"]
        for next_label in next_labels:
            lookahead_parts.append(rf"\b{re.escape(next_label)}\b")
        lookahead = "|".join(lookahead_parts)
        pattern = rf"{escaped_label}\s*:?\s*(?P<value>.*?)(?=\s*(?:{lookahead}))"
        match = re.search(pattern, flat_text, flags=re.IGNORECASE)
        if not match:
            return None
        value = (match.group("value") or "").strip(" -:\t\r\n")
        return value or None

    @staticmethod
    def _clean_description_text(value):
        text = str(value or "").strip()
        if not text:
            return None
        text = re.sub(r"\s+", " ", text).strip()
        for marker in (" QUICK OVERVIEW ", " Read More ", " See More "):
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx].strip()
        # Filter obvious non-description/script blob contamination.
        noisy_tokens = (
            '\\"',
            '{"',
            'listing-tags',
            'wpvip',
            'template\\',
            'local-logic',
            "termsOfUse",
            "stateSpecificPrivacyNotice",
        )
        lower = text.lower()
        if any(token.lower() in lower for token in noisy_tokens):
            return None
        if " - image " in lower:
            return None
        if len(text) > 2200:
            return None
        return text

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except Exception:
            try:
                return response.body.decode("utf-8", errors="replace")
            except Exception:
                return ""

    @staticmethod
    def _clean_str(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _to_int(value):
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            try:
                return int(round(float(value)))
            except (TypeError, ValueError):
                return None
        m = NUMBER_PATTERN.search(str(value))
        if not m:
            return None
        try:
            return int(round(float(m.group(0).replace(",", ""))))
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
        m = NUMBER_PATTERN.search(str(value))
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except (TypeError, ValueError):
            return None
