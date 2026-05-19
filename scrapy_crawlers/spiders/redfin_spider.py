import json
import re
from urllib.parse import unquote, urlparse

import scrapy

from spiders.env_config import build_proxy_url, get_env

NJ_COUNTIES = [
    "Atlantic", "Bergen", "Burlington", "Camden", "Cape May",
    "Cumberland", "Essex", "Gloucester", "Hudson", "Hunterdon",
    "Mercer", "Middlesex", "Monmouth", "Morris", "Ocean",
    "Passaic", "Salem", "Somerset", "Sussex", "Union", "Warren",
]

# Redfin county region IDs for New Jersey (region_type=5).
NJ_COUNTY_REGION_IDS = {
    "Atlantic": 1891,
    "Bergen": 1892,
    "Burlington": 1893,
    "Camden": 1894,
    "Cape May": 1895,
    "Cumberland": 1896,
    "Essex": 1897,
    "Gloucester": 1898,
    "Hudson": 1899,
    "Hunterdon": 1900,
    "Mercer": 1901,
    "Middlesex": 1902,
    "Monmouth": 1903,
    "Morris": 1904,
    "Ocean": 1905,
    "Passaic": 1906,
    "Salem": 1907,
    "Somerset": 1908,
    "Sussex": 1909,
    "Union": 1910,
    "Warren": 1911,
}

HOME_LINK_PATTERN = re.compile(r"/home/(\d+)")
VIEWING_PAGE_PATTERN = re.compile(r"Viewing page\s+\d+\s+of\s+(\d+)", re.IGNORECASE)
PAGE_LINK_PATTERN = re.compile(r"/page-(\d+)")
HREF_PATTERN = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
MLS_PATTERN = re.compile(r"\bMLS#?\s*([A-Za-z0-9-]+)\b", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"-?[\d,.]+")
ZIP_SUFFIX_PATTERN = re.compile(r"^(?P<street>.+)-(?P<zip>\d{5}(?:-\d{4})?)$")
TITLE_ADDRESS_PATTERN = re.compile(
    r"^\s*(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b"
)
GENERIC_ADDRESS_PATTERN = re.compile(
    r"^\s*(.+?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$"
)
YEAR_BUILT_TEXT_PATTERN = re.compile(r"\bYear Built\b\s*[:\-]?\s*((?:18|19|20)\d{2})\b", re.IGNORECASE)
PROPERTY_TYPE_TEXT_PATTERN = re.compile(
    r"\bProperty Type\b\s*[:\-]?\s*([A-Za-z][A-Za-z0-9 /&-]{2,80})",
    re.IGNORECASE,
)


class RedfinSpider(scrapy.Spider):
    name = "redfin"
    allowed_domains = ["www.redfin.com"]
    handle_httpstatus_list = [401, 403, 405, 429]
    AVM_API_URL = "https://www.redfin.com/stingray/api/home/details/avm"

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "dnt": "1",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.redfin.com/",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        },
        "DOWNLOAD_DELAY": 2,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "COOKIES_ENABLED": True,
        "CURL_IMPERSONATE": "chrome110",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {"1", "true", "yes"}
        self.proxy_url = None if disable_proxy else build_proxy_url()
        self.seen_listing_urls = set()
        max_pages_raw = kwargs.get("max_pages_per_county") or get_env(
            "REDFIN_MAX_PAGES_PER_COUNTY", default="800"
        )
        try:
            self.max_pages_per_county = int(max_pages_raw)
        except (TypeError, ValueError):
            self.max_pages_per_county = 800

        max_counties_raw = kwargs.get("max_counties") or get_env("REDFIN_MAX_COUNTIES")
        try:
            self.max_counties = int(max_counties_raw) if max_counties_raw else None
        except (TypeError, ValueError):
            self.max_counties = None

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    @staticmethod
    def _county_slug(county):
        return f"{county.replace(' ', '-')}-County"

    def _county_page_url(self, county, region_id, page):
        base = f"https://www.redfin.com/county/{region_id}/NJ/{self._county_slug(county)}"
        if page <= 1:
            return base
        return f"{base}/page-{page}"

    async def start(self):
        if self.proxy_url:
            self.logger.info("Using DataImpulse rotating proxy for Redfin requests")
        else:
            self.logger.warning("Proxy disabled/missing; running Redfin without proxy")
        self.logger.info("Redfin spider source: %s", __file__)
        self.logger.info("County mode: HTML pagination + per-property detail follow")
        yield scrapy.Request(
            "https://www.redfin.com/",
            callback=self.parse_home_page,
            errback=self.handle_home_page_error,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def handle_home_page_error(self, failure):
        self.logger.warning("Warmup request failed: %s", failure.value)
        yield from self._schedule_county_pages()

    def parse_home_page(self, response):
        self.logger.info(
            "Main page status=%d max_pages_per_county=%d",
            response.status,
            self.max_pages_per_county,
        )
        yield from self._schedule_county_pages()

    def _schedule_county_pages(self):
        counties = NJ_COUNTIES[: self.max_counties] if self.max_counties else NJ_COUNTIES
        for county in counties:
            region_id = NJ_COUNTY_REGION_IDS.get(county)
            if not region_id:
                self.logger.warning("Missing static region_id mapping for county: %s", county)
                continue
            yield scrapy.Request(
                self._county_page_url(county=county, region_id=region_id, page=1),
                callback=self.parse_county_page,
                cb_kwargs={"county": county, "region_id": region_id, "page": 1},
                meta=self._proxy_meta(),
                dont_filter=True,
            )

    def parse_county_page(self, response, county, region_id, page):
        if response.status in (401, 403, 405, 429):
            self.logger.warning(
                "County page blocked for %s page %d with status %d",
                county,
                page,
                response.status,
            )
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.warning("Empty/non-text county page for %s page %d", county, page)
            return

        hrefs = [
            href for href in HREF_PATTERN.findall(response_text)
            if "/home/" in href
        ]
        page_urls = []
        for href in hrefs:
            if not href:
                continue
            if href.startswith("http"):
                full_url = href
            elif href.startswith("/"):
                full_url = f"https://www.redfin.com{href}"
            else:
                continue
            if "/NJ/" not in full_url or "/home/" not in full_url:
                continue
            page_urls.append(full_url)

        unique_page_urls = list(dict.fromkeys(page_urls))
        self.logger.info(
            "County %s page %d extracted %d listing URLs",
            county,
            page,
            len(unique_page_urls),
        )

        for detail_url in unique_page_urls:
            if detail_url in self.seen_listing_urls:
                continue
            self.seen_listing_urls.add(detail_url)
            listing_id = self._extract_listing_id(detail_url)
            item = self._blank_item(
                county=county,
                region_id=region_id,
                page=page,
                listing_id=listing_id,
                detail_url=detail_url,
            )
            parsed_address, parsed_city, parsed_state, parsed_postal = self._parse_location_from_detail_url(detail_url)
            item["address"] = self._clean_str(parsed_address)
            item["city"] = self._clean_str(parsed_city)
            item["state"] = self._clean_str(parsed_state)
            item["postal_code"] = self._clean_str(parsed_postal)
            yield self._avm_request(item)

        total_pages = self._extract_total_pages(response_text)
        if total_pages is None:
            total_pages = page

        if page < min(total_pages, self.max_pages_per_county):
            next_page = page + 1
            yield scrapy.Request(
                self._county_page_url(county=county, region_id=region_id, page=next_page),
                callback=self.parse_county_page,
                cb_kwargs={"county": county, "region_id": region_id, "page": next_page},
                meta=self._proxy_meta(),
                dont_filter=True,
            )

    def _property_request(self, item):
        detail_url = item.get("detail_url")
        if not detail_url:
            return item
        return scrapy.Request(
            detail_url,
            callback=self.parse_property_detail,
            meta={"base_item": item, **self._proxy_meta()},
            dont_filter=True,
        )

    def _avm_request(self, item):
        property_id = item.get("listing_id")
        if not property_id:
            return self._property_request(item)
        url = (
            f"{self.AVM_API_URL}"
            f"?propertyId={property_id}&accessLevel=1&pageType=1"
        )
        return scrapy.Request(
            url,
            callback=self.parse_avm_detail,
            headers={
                "accept": "*/*",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "x-requested-with": "XMLHttpRequest",
                "referer": item.get("detail_url") or "https://www.redfin.com/",
            },
            meta={"base_item": item, **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_avm_detail(self, response):
        item = dict(response.meta.get("base_item") or {})
        if response.status == 200:
            payload = self._parse_avm_payload(response)
            if payload:
                self._enrich_item_from_avm_payload(item, payload)
        yield self._property_request(item)

    def parse_property_detail(self, response):
        item = dict(response.meta.get("base_item") or {})
        text = self._safe_response_text(response)
        if response.status != 200:
            self.logger.debug(
                "Redfin detail non-200 status=%s listing_id=%s url=%s",
                response.status,
                item.get("listing_id"),
                item.get("detail_url"),
            )
        if not text:
            yield self._as_property_item(item)
            return

        selector = scrapy.Selector(text=text)
        self._enrich_item_from_property_page(item, selector, text)
        self._enrich_item_from_text_fallbacks(item, text)
        if not item.get("address"):
            address, city, state, postal_code = self._parse_location_from_detail_url(item.get("detail_url"))
            item["address"] = item.get("address") or self._clean_str(address)
            item["city"] = item.get("city") or self._clean_str(city)
            item["state"] = item.get("state") or self._clean_str(state)
            item["postal_code"] = item.get("postal_code") or self._clean_str(postal_code)
        yield self._as_property_item(item)

    @staticmethod
    def _as_property_item(document):
        return dict(document or {})

    def _blank_item(self, county, region_id, page, listing_id, detail_url):
        return {
            "source": "redfin",
            "county": county,
            "region_id": region_id,
            "listing_id": self._clean_str(listing_id),
            "mls_id": None,
            "detail_url": self._clean_str(detail_url),
            "address": None,
            "city": None,
            "state": None,
            "postal_code": None,
            "list_price": None,
            "status": None,
            "property_type": None,
            "beds": None,
            "baths": None,
            "lot_size_sqft": None,
            "lot_size_acres": None,
            "build_area_sqft": None,
            "year_built": None,
            "stories": None,
            "latitude": None,
            "longitude": None,
            "description": None,
            "page": page,
        }

    def _enrich_item_from_property_page(self, item, selector, response_text):
        title = (selector.xpath("normalize-space(//title)").get() or "").strip()
        if title:
            self._apply_address_from_title(item, title)
            if not item.get("mls_id"):
                m = MLS_PATTERN.search(title)
                if m:
                    item["mls_id"] = self._clean_str(m.group(1))

        for obj in self._extract_json_ld_objects(selector):
            if not self._is_property_json_ld(obj):
                continue

            address = obj.get("address") if isinstance(obj, dict) else None
            if isinstance(address, dict):
                if not item.get("address"):
                    item["address"] = self._clean_str(
                        address.get("streetAddress")
                        or address.get("addressLine1")
                        or address.get("name")
                    )
                if not item.get("city"):
                    item["city"] = self._clean_str(address.get("addressLocality"))
                if not item.get("state"):
                    item["state"] = self._clean_str(address.get("addressRegion"))
                if not item.get("postal_code"):
                    item["postal_code"] = self._clean_str(address.get("postalCode"))
            elif isinstance(address, str):
                self._apply_address_from_text(item, address)

            offers = obj.get("offers") if isinstance(obj, dict) else None
            offer_candidates = offers if isinstance(offers, list) else [offers]
            for offer in offer_candidates:
                if not isinstance(offer, dict):
                    continue
                if item.get("list_price") is None:
                    item["list_price"] = self._to_int(offer.get("price"))
                if item.get("status") is None:
                    availability = self._clean_str(offer.get("availability"))
                    item["status"] = self._normalize_status(availability)

            if item.get("property_type") is None:
                ptype = obj.get("@type")
                if isinstance(ptype, list):
                    ptype = next((v for v in ptype if isinstance(v, str)), None)
                item["property_type"] = self._normalize_property_type(ptype)

            if item.get("beds") is None:
                item["beds"] = self._to_float(obj.get("numberOfBedrooms"))
            if item.get("baths") is None:
                item["baths"] = self._to_float(
                    obj.get("numberOfBathroomsTotal") or obj.get("numberOfBathrooms")
                )
            if item.get("build_area_sqft") is None:
                area = obj.get("floorSize")
                if isinstance(area, dict):
                    item["build_area_sqft"] = self._to_int(area.get("value"))
            if item.get("description") is None:
                item["description"] = self._normalize_description(obj.get("description"))
            if item.get("year_built") is None:
                item["year_built"] = self._to_int(obj.get("yearBuilt"))

            geo = obj.get("geo") if isinstance(obj, dict) else None
            if isinstance(geo, dict):
                if item.get("latitude") is None:
                    item["latitude"] = self._to_float(geo.get("latitude"))
                if item.get("longitude") is None:
                    item["longitude"] = self._to_float(geo.get("longitude"))

        if not item.get("mls_id"):
            m = MLS_PATTERN.search(response_text or "")
            if m:
                item["mls_id"] = self._clean_str(m.group(1))

        self._enrich_item_from_next_data(item, selector)

    def _parse_avm_payload(self, response):
        text = self._safe_response_text(response).strip()
        if not text:
            return None
        if text.startswith("{}&&"):
            text = text[4:]
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        payload = parsed.get("payload")
        return payload if isinstance(payload, dict) else None

    def _enrich_item_from_avm_payload(self, item, payload):
        if item.get("list_price") is None:
            item["list_price"] = self._to_int(
                payload.get("listingPrice")
                or self._find_first_value(payload, {"listPrice", "price"})
            )
        if item.get("beds") is None:
            item["beds"] = self._to_float(
                payload.get("numBeds")
                or self._find_first_value(payload, {"beds", "numBeds", "bedrooms", "numberOfBedrooms"})
            )
        if item.get("baths") is None:
            item["baths"] = self._to_float(
                payload.get("numBaths")
                or self._find_first_value(
                    payload,
                    {"baths", "numBaths", "bathrooms", "numberOfBathrooms", "numberOfBathroomsTotal"},
                )
            )

        if item.get("build_area_sqft") is None:
            sqft = payload.get("sqFt")
            if isinstance(sqft, dict):
                item["build_area_sqft"] = self._to_int(sqft.get("value"))
            else:
                item["build_area_sqft"] = self._to_int(
                    sqft
                    or self._find_first_value(
                        payload,
                        {"sqft", "sqFt", "finishedSqFt", "livingArea", "buildingAreaTotal"},
                    )
                )

        if item.get("lot_size_sqft") is None:
            item["lot_size_sqft"] = self._to_int(
                self._find_first_value(payload, {"lotSizeSqFt", "lotsize", "lotSqFt", "lotSize"})
            )
        if item.get("lot_size_acres") is None and item.get("lot_size_sqft") is not None:
            item["lot_size_acres"] = round(float(item["lot_size_sqft"]) / 43560.0, 4)

        if item.get("year_built") is None:
            item["year_built"] = self._to_int(
                payload.get("yearBuilt")
                or self._find_first_value(
                    payload,
                    {"yearBuilt", "builtYear", "yrBuilt", "propertyYearBuilt", "yearBuiltValue"},
                )
            )
        if self._is_unusable_property_type(item.get("property_type")):
            item["property_type"] = self._normalize_property_type(
                self._find_first_value(payload, {"propertyType", "homeType", "propertySubType", "style"})
            )
        if self._is_unusable_status(item.get("status")):
            item["status"] = self._normalize_status(
                self._find_first_value(payload, {"listingStatus", "propertyStatus", "status", "homeStatus", "mlsStatus"})
            )
        if self._is_unusable_description(item.get("description")):
            item["description"] = self._normalize_description(
                self._find_first_value(payload, {"publicRemarks", "remarks", "marketingRemarks", "description"})
            )
        if item.get("mls_id") is None:
            item["mls_id"] = self._clean_str(
                self._find_first_value(payload, {"mlsId", "mlsNumber", "listingMlsId"})
            )

        lat_long = payload.get("latLong")
        if isinstance(lat_long, dict):
            if item.get("latitude") is None:
                item["latitude"] = self._to_float(lat_long.get("latitude"))
            if item.get("longitude") is None:
                item["longitude"] = self._to_float(lat_long.get("longitude"))
        else:
            if item.get("latitude") is None:
                item["latitude"] = self._to_float(self._find_first_value(payload, {"latitude", "lat"}))
            if item.get("longitude") is None:
                item["longitude"] = self._to_float(self._find_first_value(payload, {"longitude", "lng", "lon"}))

        street_address = payload.get("streetAddress")
        if isinstance(street_address, dict):
            assembled = street_address.get("assembledAddress")
            self._apply_address_fields(
                item,
                street=self._clean_str(
                    assembled
                    or street_address.get("streetAddress")
                    or street_address.get("address")
                    or street_address.get("addressLine1")
                ),
                city=self._clean_str(street_address.get("city") or street_address.get("addressLocality")),
                state=self._clean_str(street_address.get("state") or street_address.get("addressRegion")),
                postal_code=self._clean_str(street_address.get("zip") or street_address.get("postalCode")),
            )
        elif isinstance(street_address, str):
            self._apply_address_from_text(item, street_address)

        if not item.get("address"):
            self._apply_address_fields(
                item,
                street=self._clean_str(
                    self._find_first_value(payload, {"addressLine1", "street", "streetName"})
                ),
                city=self._clean_str(self._find_first_value(payload, {"city", "addressLocality"})),
                state=self._clean_str(self._find_first_value(payload, {"state", "addressRegion"})),
                postal_code=self._clean_str(self._find_first_value(payload, {"zip", "postalCode"})),
            )

    def _enrich_item_from_next_data(self, item, selector):
        raw = selector.xpath("string(//script[@id='__NEXT_DATA__'])").get()
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return

        if item.get("list_price") is None:
            item["list_price"] = self._to_int(
                self._find_first_value(payload, {"listPrice", "price", "askingPrice", "displayPrice"})
            )
        if item.get("beds") is None:
            item["beds"] = self._to_float(
                self._find_first_value(payload, {"beds", "numBeds", "bedrooms", "numberOfBedrooms"})
            )
        if item.get("baths") is None:
            item["baths"] = self._to_float(
                self._find_first_value(
                    payload,
                    {"baths", "numBaths", "bathrooms", "numberOfBathrooms", "numberOfBathroomsTotal"},
                )
            )
        if item.get("build_area_sqft") is None:
            item["build_area_sqft"] = self._to_int(
                self._find_first_value(payload, {"sqFt", "sqft", "livingArea", "buildingAreaTotal", "finishedSqFt"})
            )
        if item.get("lot_size_sqft") is None:
            item["lot_size_sqft"] = self._to_int(
                self._find_first_value(payload, {"lotSizeSqFt", "lotSqFt", "lotSize", "lotSizeValue"})
            )
        if item.get("lot_size_acres") is None and item.get("lot_size_sqft") is not None:
            item["lot_size_acres"] = round(float(item["lot_size_sqft"]) / 43560.0, 4)
        if item.get("year_built") is None:
            item["year_built"] = self._to_int(
                self._find_first_value(payload, {"yearBuilt", "builtYear", "yrBuilt", "propertyYearBuilt", "yearBuiltValue"})
            )
        if self._is_unusable_property_type(item.get("property_type")):
            item["property_type"] = self._normalize_property_type(
                self._find_first_value(payload, {"propertyType", "homeType", "propertySubType", "style", "propertyTypeName"})
            )
        if self._is_unusable_status(item.get("status")):
            item["status"] = self._normalize_status(
                self._find_first_value(payload, {"listingStatus", "propertyStatus", "status", "homeStatus", "mlsStatus"})
            )
        if self._is_unusable_description(item.get("description")):
            item["description"] = self._normalize_description(
                self._find_first_value(payload, {"description", "publicRemarks", "remarks", "marketingRemarks"})
            )
        if item.get("mls_id") is None:
            item["mls_id"] = self._clean_str(
                self._find_first_value(payload, {"mlsId", "mlsNumber", "listingMlsId"})
            )
        if item.get("latitude") is None:
            item["latitude"] = self._to_float(self._find_first_value(payload, {"latitude", "lat"}))
        if item.get("longitude") is None:
            item["longitude"] = self._to_float(self._find_first_value(payload, {"longitude", "lng", "lon"}))
        address_obj = self._find_first_value(payload, {"address", "streetAddress"})
        if isinstance(address_obj, dict):
            self._apply_address_fields(
                item,
                street=self._clean_str(
                    address_obj.get("streetAddress")
                    or address_obj.get("addressLine1")
                    or address_obj.get("address")
                    or address_obj.get("name")
                ),
                city=self._clean_str(address_obj.get("addressLocality") or address_obj.get("city")),
                state=self._clean_str(address_obj.get("addressRegion") or address_obj.get("state")),
                postal_code=self._clean_str(address_obj.get("postalCode") or address_obj.get("zip")),
            )
        elif isinstance(address_obj, str):
            self._apply_address_from_text(item, address_obj)
        else:
            self._apply_address_fields(
                item,
                street=self._clean_str(
                    self._find_first_value(payload, {"addressLine1", "street", "streetName"})
                ),
                city=self._clean_str(self._find_first_value(payload, {"city", "addressLocality"})),
                state=self._clean_str(self._find_first_value(payload, {"state", "addressRegion"})),
                postal_code=self._clean_str(self._find_first_value(payload, {"zip", "postalCode"})),
            )

    def _enrich_item_from_text_fallbacks(self, item, response_text):
        text = response_text or ""
        if item.get("year_built") is None:
            m = YEAR_BUILT_TEXT_PATTERN.search(text)
            if m:
                item["year_built"] = self._to_int(m.group(1))
        if self._is_unusable_property_type(item.get("property_type")):
            m = PROPERTY_TYPE_TEXT_PATTERN.search(text)
            if m:
                item["property_type"] = self._normalize_property_type(m.group(1))

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
            yield from RedfinSpider._walk_json_ld_nodes(parsed)

    @staticmethod
    def _walk_json_ld_nodes(node):
        if isinstance(node, dict):
            yield node
            graph = node.get("@graph")
            if isinstance(graph, list):
                for child in graph:
                    yield from RedfinSpider._walk_json_ld_nodes(child)
            return
        if isinstance(node, list):
            for child in node:
                yield from RedfinSpider._walk_json_ld_nodes(child)

    @staticmethod
    def _is_property_json_ld(obj):
        if not isinstance(obj, dict):
            return False
        types = obj.get("@type")
        if isinstance(types, str):
            type_values = {types.lower()}
        elif isinstance(types, list):
            type_values = {str(v).lower() for v in types if v}
        else:
            type_values = set()

        property_type_tokens = {
            "singlefamilyresidence",
            "residence",
            "house",
            "apartment",
            "realestatelisting",
            "accommodation",
            "product",
        }
        if type_values.intersection(property_type_tokens):
            return True

        if isinstance(obj.get("address"), dict):
            return True
        if obj.get("numberOfBedrooms") or obj.get("numberOfBathrooms") or obj.get("numberOfBathroomsTotal"):
            return True
        offers = obj.get("offers")
        if isinstance(offers, dict) and offers.get("price"):
            return True
        if isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict) and offer.get("price"):
                    return True
        return False

    def _apply_address_from_title(self, item, title):
        m = TITLE_ADDRESS_PATTERN.match((title or "").strip())
        if not m:
            return
        address, city, state, postal_code = m.groups()
        if not item.get("address"):
            item["address"] = self._clean_str(address)
        if not item.get("city"):
            item["city"] = self._clean_str(city)
        if not item.get("state"):
            item["state"] = self._clean_str(state)
        if not item.get("postal_code"):
            item["postal_code"] = self._clean_str(postal_code)

    @staticmethod
    def _extract_listing_id(url):
        m = HOME_LINK_PATTERN.search(url or "")
        return m.group(1) if m else None

    def _parse_location_from_detail_url(self, detail_url):
        value = str(detail_url or "").strip()
        if not value:
            return None, None, None, None

        parsed = urlparse(value)
        parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
        # Typical format:
        # /NJ/City/Street-Name-07652/home/123
        if len(parts) < 4:
            return None, None, None, None

        state = parts[0].upper()
        city = parts[1].replace("-", " ").strip() or None
        if city:
            city = " ".join(token.capitalize() for token in city.split())

        street = None
        postal_code = None
        middle_segments = []
        for segment in parts[2:]:
            if segment == "home":
                break
            middle_segments.append(segment)

        for segment in middle_segments:
            m = ZIP_SUFFIX_PATTERN.match(segment)
            if not m:
                continue
            street_slug = m.group("street")
            postal_code = m.group("zip")
            street = street_slug.replace("-", " ").strip() or None
            break

        if not street and middle_segments:
            fallback = middle_segments[0].replace("-", " ").strip()
            street = fallback or None

        return street, city, state, postal_code

    def _apply_address_fields(self, item, street=None, city=None, state=None, postal_code=None):
        if street and not item.get("address"):
            item["address"] = self._clean_str(street)
        if city and not item.get("city"):
            item["city"] = self._clean_str(city)
        if state and not item.get("state"):
            item["state"] = self._clean_str(state)
        if postal_code and not item.get("postal_code"):
            item["postal_code"] = self._clean_str(postal_code)

    def _apply_address_from_text(self, item, text):
        value = self._clean_str(text)
        if not value:
            return
        m = GENERIC_ADDRESS_PATTERN.match(value)
        if m:
            street, city, state, postal_code = m.groups()
            self._apply_address_fields(item, street=street, city=city, state=state, postal_code=postal_code)
            return
        if not item.get("address"):
            item["address"] = value

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
    def _extract_total_pages(response_text):
        m = VIEWING_PAGE_PATTERN.search(response_text or "")
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None

        page_numbers = [
            int(p) for p in PAGE_LINK_PATTERN.findall(response_text or "") if p.isdigit()
        ]
        if page_numbers:
            return max(page_numbers)
        return None

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
            return int(round(float(value)))
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
            return float(value)
        m = NUMBER_PATTERN.search(str(value))
        if not m:
            return None
        try:
            return float(m.group(0).replace(",", ""))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_status(raw):
        if isinstance(raw, dict):
            value = None
            for key in (
                "displayValue",
                "DISPLAYVALUE",
                "longerDefinitionToken",
                "LONGERDEFINITIONTOKEN",
                "status",
                "name",
                "value",
                "definition",
                "DEFINITION",
            ):
                candidate = raw.get(key)
                if candidate not in (None, ""):
                    value = str(candidate).strip()
                    break
            if not value:
                value = ""
        else:
            value = str(raw).strip() if raw is not None else ""
        if not value:
            return None
        value = value.split("/")[-1]
        normalized = value.replace("_", " ").upper()
        status_map = {
            "IN STOCK": "FOR SALE",
            "OUT OF STOCK": "OFF MARKET",
            "FORSALE": "FOR SALE",
            "FOR SALE": "FOR SALE",
            "PENDING": "PENDING",
            "CONTINGENT": "CONTINGENT",
            "SOLD": "SOLD",
            "CLOSED": "SOLD",
            "ACTIVE": "FOR SALE",
            "COMING SOON": "COMING SOON",
            "OFF MARKET": "OFF MARKET",
        }
        return status_map.get(normalized, normalized)

    @staticmethod
    def _normalize_property_type(raw):
        value = str(raw).strip() if raw is not None else ""
        if not value:
            return None
        cleaned = value.replace("_", " ").strip()
        token = cleaned.lower()
        if token.isdigit():
            numeric_map = {
                "1": "Single Family",
                "2": "Condo",
                "3": "Townhouse",
                "4": "Multi Family",
                "5": "Land",
                "6": "Single Family",
            }
            return numeric_map.get(token)
        type_map = {
            "singlefamilyresidence": "Single Family",
            "single-family": "Single Family",
            "single family residential": "Single Family",
            "single family": "Single Family",
            "single family residence": "Single Family",
            "townhouse": "Townhouse",
            "townhome": "Townhouse",
            "condo": "Condo",
            "condo/coop": "Condo",
            "condo/co-op": "Condo",
            "condominium": "Condo",
            "apartment": "Apartment",
            "manufactured": "Manufactured Home",
            "mobile": "Manufactured Home",
            "duplex": "Duplex",
            "triplex": "Triplex",
            "quadruplex": "Quadruplex",
            "multifamily": "Multi Family",
            "multi family": "Multi Family",
            "land": "Land",
            "lot": "Land",
        }
        if token in {"product", "realestatelisting", "accommodation", "residence"}:
            return None
        return type_map.get(token, cleaned.title())

    @staticmethod
    def _normalize_description(raw):
        text = RedfinSpider._clean_str(raw)
        if not text:
            return None
        compact = re.sub(r"\s+", " ", text).strip()
        lower = compact.lower()
        # Drop broker/source placeholders that are not property remarks.
        if ("mls" in lower and len(compact) <= 80) or lower in {
            "central jersey mls",
            "garden state mls",
            "new jersey mls",
        }:
            return None
        return compact

    @staticmethod
    def _is_unusable_status(value):
        if value in (None, ""):
            return True
        text = str(value).strip()
        if not text:
            return True
        return text.startswith("{") and "displayvalue" in text.lower()

    @staticmethod
    def _is_unusable_property_type(value):
        if value in (None, ""):
            return True
        return str(value).strip().isdigit()

    @staticmethod
    def _is_unusable_description(value):
        if value in (None, ""):
            return True
        return RedfinSpider._normalize_description(value) is None

    @staticmethod
    def _find_first_value(node, keys):
        key_set = {str(k).strip().lower() for k in (keys or set()) if str(k).strip()}
        if not key_set:
            return None
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if isinstance(key, str) and key.strip().lower() in key_set:
                        if value not in (None, "", [], {}):
                            return value
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                for value in current:
                    if isinstance(value, (dict, list)):
                        stack.append(value)
        return None
