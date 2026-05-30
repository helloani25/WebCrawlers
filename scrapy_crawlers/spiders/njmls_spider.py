import json
import re
import time
from urllib.parse import urlencode, urljoin

import scrapy

from spiders.env_config import build_proxy_url, get_env

HOMEPAGE_URL = "https://www.njmls.com/"
RESULTS_URL = "https://www.njmls.com/listings/index.cfm"
TOWN_SELECT_NEW_URL = "https://www.njmls.com/listings/index.cfm?action=xhr.multiple_town_select_new"
SEARCH_URL = "https://www.njmls.com/listings/index.cfm?action=dsp.search"

# TODO (community info enrichment):
# - Add optional spider arg: include_community_info=1
# - Queue MLS properties in SQLite for community enrichment:
#   pending -> in_progress -> done/failed
# - Build request URL:
#   https://www.njmls.com/communities/index.cfm
#     ?action=dsp.towninfo
#     &townname=<TOWN>
#     &view=facts
#     &mlsnum=<MLSNUM>
#     &county=<COUNTY>
# - Amenities example:
#   https://www.njmls.com/communities/index.cfm?action=dsp.towninfo&townname=SADDLE%20BROOK&view=amenities&mlsnum=26015947&county=BERGEN
# - Parse structured sections only:
#   demographics, schools, amenities, public transit
# - For amenities, extract nearby + popular entities for categories like
#   banks, restaurants, gas stations, pharmacies, groceries, hospitals.
# - Exclude disclaimer/weather blocks from extracted text.
# - Limit amenities to top 20 entities per category.
# - Later add property tax history and sale history extraction.

# All 21 NJ counties in NJMLS (lowercase, matching the county= query param)
NJ_COUNTIES = [
    "atlantic",
    "bergen",
    "burlington",
    "camden",
    "cape may",
    "cumberland",
    "essex",
    "gloucester",
    "hudson",
    "hunterdon",
    "mercer",
    "middlesex",
    "monmouth",
    "morris",
    "ocean",
    "passaic",
    "salem",
    "somerset",
    "sussex",
    "union",
    "warren",
]

MLS_PATTERN = re.compile(r"\b(\d{8})\b")
PRICE_PATTERN = re.compile(r"\$\s*([\d,]+)")
CITY_STATE_ZIP_PATTERN = re.compile(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$")
ADDRESS_CITY_STATE_OPTIONAL_ZIP_PATTERN = re.compile(
    r"^(.*?),\s*([^,]+),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$"
)
DETAIL_TITLE_ADDRESS_PATTERN = re.compile(
    r"-\s*([^,\n]+?),\s*([^,\n]+),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?\s*-\s*New Jersey Multiple Listing Service",
    re.I,
)
BEDS_PATTERN = re.compile(r"\b(\d{1,2})\s*(?:bd|br|bed(?:room)?s?)\b", re.I)
BATHS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ba|bath|BA|Bath)", re.I)
SQFT_PATTERN = re.compile(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|SF)", re.I)
DISPLAY_COUNT_PATTERN = re.compile(r"Showing\s+(?:1\s*-\s*)?(\d+)\s+(?:of\s+)?(\d+)", re.I)
ZIP_CODE_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")
FULL_BATHS_PATTERN = re.compile(r"(\d+)\s*full\s*bath", re.I)
HALF_BATHS_PATTERN = re.compile(r"(\d+)\s*half\s*bath", re.I)
GARAGE_SPACES_PATTERN = re.compile(r"(\d+)\s*(?:car\s+)?garage", re.I)
GARAGE_PARKING_PATTERN = re.compile(r"(\d+)\s+parking\s+spaces?\s+in\s+the\s+garage", re.I)
YEAR_BUILT_PATTERN = re.compile(r"\b(1[89]\d{2}|20\d{2})(?:'s|s)?\b")
TAX_YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
TAX_YEAR_CONTEXT_PATTERN = re.compile(
    r"\btax(?:es)?(?:\s+year)?\b[^\d]{0,20}(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)
DAYS_ON_MARKET_PATTERN = re.compile(
    r"\b(?:days?\s+on\s+(?:the\s+)?market|dom)\b[^0-9]{0,12}(\d{1,4})\b",
    re.I,
)
MLS_IN_URL_PATTERN = re.compile(r"mlsnum=(\d{8})", re.I)
EMAIL_PATTERN = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?:\+?1[\s.\-\u2010-\u2015]?)?(?:\(?\d{3}\)?[\s.\-\u2010-\u2015]?)\d{3}[\s.\-\u2010-\u2015]?\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d+)?",
    re.I,
)
PHOTO_URL_PATTERN = re.compile(r'https?://[^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?', re.I)
STYLE_URL_PATTERN = re.compile(r"url\((['\"]?)([^)\"']+)\\1\)", re.I)
UPPERCASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWERCASE = "abcdefghijklmnopqrstuvwxyz"


class NjmlsSpider(scrapy.Spider):
    name = "njmls"
    allowed_domains = ["www.njmls.com"]
    handle_httpstatus_list = [401, 403, 405, 429]

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "DNT": "1",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        },
        "COOKIES_ENABLED": True,
        "DOWNLOAD_DELAY": 0.8,
        "RANDOMIZE_DOWNLOAD_DELAY" : True,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 6,
        "CURL_IMPERSONATE": "chrome110",
    }

    DISPLAY_PER_PAGE = 30

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {"1", "true", "yes"}
        self.proxy_url = None if disable_proxy else build_proxy_url()
        self.scrapfly_api_key = (
            kwargs.get("scrapfly_api_key")
            or get_env("SCRAPFLY_API_KEY", "SCRAPFLY_KEY")
            or ""
        ).strip()
        self.scrapfly_asp_enabled = self._is_truthy(
            kwargs.get("scrapfly_asp_enabled"),
            get_env("NJMLS_SCRAPFLY_ASP_ENABLED", "SCRAPFLY_ASP_ENABLED", default="0"),
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
        self.seen_mls_ids = set()
        self.max_counties = self._safe_int(
            kwargs.get("max_counties"), get_env("NJMLS_MAX_COUNTIES")
        )

    @staticmethod
    def _safe_int(value, fallback=None):
        candidate = value if value not in (None, "") else fallback
        try:
            return int(candidate) if candidate not in (None, "") else None
        except (TypeError, ValueError):
            return None

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
            self.logger.info("Using proxy for NJMLS requests")
        else:
            self.logger.warning("No proxy configured; running without a proxy")
        if self.scrapfly_asp_enabled and self.scrapfly_api_key:
            self.logger.info("Scrapfly ASP fallback enabled for NJMLS blocked detail responses")
        elif self.scrapfly_asp_enabled:
            self.logger.warning("Scrapfly ASP enabled but SCRAPFLY_API_KEY is missing; fallback disabled")
        yield scrapy.Request(
            HOMEPAGE_URL,
            callback=self.parse_homepage,
            meta=self._proxy_meta(),
            dont_filter=True,
            headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
        )

    def parse_homepage(self, response):
        """Prime homepage cookies then open search page for listing session context."""
        self.logger.info("Homepage loaded status=%s; opening search page", response.status)
        yield scrapy.Request(
            SEARCH_URL,
            callback=self.parse_search_page,
            meta=self._proxy_meta(),
            dont_filter=True,
            headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
        )

    def parse_search_page(self, response):
        """Open the multi-town iframe payload, then shard searches by parsed towns."""
        self.logger.info("Search page loaded status=%s; loading town modal payload", response.status)
        text = self._safe_response_text(response)
        selector = scrapy.Selector(text=text) if text else scrapy.Selector(text="")
        town_href = selector.xpath('//a[contains(@class, "open_multi_town_new")]/@href').get()
        if not town_href:
            town_href = "/listings/index.cfm?action=xhr.multiple_town_select_new"
        town_url = response.urljoin(town_href)

        counties = NJ_COUNTIES
        if self.max_counties:
            counties = counties[: self.max_counties]
        yield scrapy.Request(
            town_url,
            callback=self.parse_town_modal,
            headers={"Referer": response.url},
            meta={"counties": counties, **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_town_modal(self, response):
        """Parse all counties/cities from iframe payload; stage requests as ZIP -> town -> county."""
        counties = response.meta.get("counties") or NJ_COUNTIES
        if response.status != 200:
            self.logger.warning(
                "Town modal payload failed status=%s; using county-wide fallback",
                response.status,
            )
            for county in counties:
                yield from self._results_request(county=county, page=1, city="", zipcode="", stage="county")
            return

        text = self._safe_response_text(response)
        if not text or not text.strip():
            self.logger.warning("Empty town modal payload; using county-wide fallback")
            for county in counties:
                yield from self._results_request(county=county, page=1, city="", zipcode="", stage="county")
            return

        selector = scrapy.Selector(text=text)
        for county in counties:
            zip_values = self._extract_zip_values_for_county(selector, county)
            if zip_values:
                self.logger.info("County=%s zip shards=%s", county, len(zip_values))
                for zip_code in zip_values:
                    yield from self._results_request(
                        county=county,
                        page=1,
                        city="",
                        zipcode=zip_code,
                        stage="zip",
                    )
            else:
                self.logger.info("No ZIP values found for county=%s", county)

            city_values = self._extract_city_values_for_county(selector, county)
            if city_values:
                self.logger.info("County=%s town shards=%s endpoint=new", county, len(city_values))
                for city in city_values:
                    yield from self._results_request(
                        county=county,
                        page=1,
                        city=city,
                        zipcode="",
                        stage="town",
                    )
            else:
                self.logger.info("No town values found for county=%s", county)

            # Always run a final county-wide shard after zip/town stages.
            yield from self._results_request(county=county, page=1, city="", zipcode="", stage="county")

    def _extract_city_values_for_county(self, selector, county):
        city_values = []
        county_phrase = f"{county.lower()} county"

        # Preferred format in current HAR: checkbox rows grouped under "<COUNTY> COUNTY, NJ".
        town_inputs = selector.xpath(
            f'//h3[a[contains(translate(normalize-space(string(.)), "{UPPERCASE}", "{LOWERCASE}"), "{county_phrase}")]]'
            f'/following-sibling::div[1]//input[@type="checkbox"]/@value'
        ).getall()

        for val in town_inputs:
            # Example: "BERKELEY HEIGHTS, NJ, UNION, 07922"
            parts = [p.strip() for p in (val or "").split(",")]
            if not parts:
                continue
            city_name = parts[0]
            if city_name:
                city_values.append(city_name)

        # Fallback for checkbox payloads when county sections are not present.
        if not city_values:
            county_norm = county.replace(" ", "").upper()
            for val in selector.xpath('//input[@type="checkbox"]/@value').getall():
                parts = [p.strip() for p in (val or "").split(",")]
                if len(parts) < 3:
                    continue
                city_name, county_token = parts[0], parts[2]
                if county_token.replace(" ", "").upper() != county_norm:
                    continue
                if city_name:
                    city_values.append(city_name)

        # Last fallback for legacy option-based payloads.
        if not city_values:
            for val in selector.xpath('//option/@value').getall():
                value = (val or "").strip()
                if not value:
                    continue
                lower = value.lower()
                if lower in {"all", "any"}:
                    continue
                city_values.append(value)

        # Deduplicate while preserving order.
        seen = set()
        return [v for v in city_values if not (v in seen or seen.add(v))]

    def _extract_zip_values_for_county(self, selector, county):
        zip_values = []
        county_phrase = f"{county.lower()} county"

        town_inputs = selector.xpath(
            f'//h3[a[contains(translate(normalize-space(string(.)), "{UPPERCASE}", "{LOWERCASE}"), "{county_phrase}")]]'
            f'/following-sibling::div[1]//input[@type="checkbox"]/@value'
        ).getall()

        for val in town_inputs:
            parts = [p.strip() for p in (val or "").split(",")]
            if len(parts) < 4:
                continue
            zip_code = parts[3]
            if zip_code and ZIP_CODE_PATTERN.match(zip_code):
                zip_values.append(zip_code)

        if not zip_values:
            county_norm = county.replace(" ", "").upper()
            for val in selector.xpath('//input[@type="checkbox"]/@value').getall():
                parts = [p.strip() for p in (val or "").split(",")]
                if len(parts) < 4:
                    continue
                county_token = parts[2]
                zip_code = parts[3]
                if county_token.replace(" ", "").upper() != county_norm:
                    continue
                if zip_code and ZIP_CODE_PATTERN.match(zip_code):
                    zip_values.append(zip_code)

        seen = set()
        return [v for v in zip_values if not (v in seen or seen.add(v))]

    def _results_request(self, county, page, city="", zipcode="", stage="unknown"):
        ts = int(time.time() * 1000)
        params = [
            ("zoomlevel", "0"),
            ("action", "xhr.results.view.rerunphoto"),
            ("page", str(page)),
            ("display", str(self.DISPLAY_PER_PAGE)),
            ("sortBy", "newest"),
            ("isFuzzy", "false"),
            ("location", ""),
            ("city", city or ""),
            ("state", "NJ"),
            ("county", county),
            ("zipcode", zipcode or ""),
            ("radius", ""),
            ("proptype", ""),
            ("maxprice", ""),
            ("minprice", ""),
            ("beds", "0"),
            ("baths", "0"),
            ("dayssince", ""),
            ("newlistings", ""),
            ("pricechanged", ""),
            ("keywords", ""),
            ("mls_number", ""),
            ("garage", ""),
            ("basement", ""),
            ("fireplace", ""),
            ("pool", ""),
            ("laundry", ""),
            ("elevator", ""),
            ("fitnesscenter", ""),
            ("furnished", ""),
            ("shortterm", ""),
            ("dogsallowed", ""),
            ("catsallowed", ""),
            ("earliestdate", ""),
            ("latestdate", ""),
            ("yearBuilt", ""),
            ("building", ""),
            ("officeID", ""),
            ("openhouse", ""),
            ("countysearch", "true"),
            ("ohdate", ""),
            ("style", ""),
            ("rerun", ""),
            ("rerundate", ""),
            ("searchname", ""),
            ("backtosearch", "false"),
            ("token", "false"),
            ("searchid", ""),
            ("searchcountid", ""),
            ("emailalert_yn", "I"),
            ("status", "A"),
            ("_", str(ts)),
        ]
        qs = "&".join(f"{k}={v}" for k, v in params)
        url = f"{RESULTS_URL}?{qs}"
        county_slug = county.replace(" ", "-")
        referer = f"https://www.njmls.com/{county_slug}-county-nj-property"
        yield scrapy.Request(
            url,
            callback=self.parse_results,
            headers={"Referer": referer},
            meta={
                "county": county,
                "city": city or "",
                "zipcode": zipcode or "",
                "stage": stage or "unknown",
                "page": page,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def parse_results(self, response):
        county = response.meta["county"]
        city = response.meta.get("city", "")
        zipcode = response.meta.get("zipcode", "")
        stage = response.meta.get("stage", "unknown")
        page = response.meta["page"]

        if response.status != 200:
            self.logger.warning(
                "Results failed stage=%s county=%s city=%s zip=%s page=%s status=%s",
                stage,
                county,
                city or "-",
                zipcode or "-",
                page,
                response.status,
            )
            return

        text = self._safe_response_text(response)
        if not text or not text.strip():
            self.logger.warning(
                "Empty response stage=%s county=%s city=%s zip=%s page=%s",
                stage,
                county,
                city or "-",
                zipcode or "-",
                page,
            )
            return

        selector = scrapy.Selector(text=text)
        listing_nodes = self._get_listing_nodes(selector)
        new_count = 0

        for node in listing_nodes:
            listing = self._parse_listing_card(node, county)
            if not listing:
                continue
            mls_id = listing.get("mls_id")
            if not mls_id:
                continue
            if mls_id in self.seen_mls_ids:
                continue
            self.seen_mls_ids.add(mls_id)
            new_count += 1
            detail_url = listing.get("detail_url")
            if detail_url:
                yield scrapy.Request(
                    detail_url,
                    callback=self.parse_listing_detail,
                    headers={"Referer": response.url},
                    meta={"listing": listing, **self._proxy_meta()},
                    dont_filter=True,
                )
            else:
                yield listing

        self.logger.info(
            "NJMLS stage=%s county=%s city=%s zip=%s page=%s nodes=%s new=%s",
            stage,
            county,
            city or "-",
            zipcode or "-",
            page,
            len(listing_nodes),
            new_count,
        )

        # Paginate when a full page was returned
        if len(listing_nodes) >= self.DISPLAY_PER_PAGE:
            yield from self._results_request(
                county=county,
                page=page + 1,
                city=city,
                zipcode=zipcode,
                stage=stage,
            )

    def _get_listing_nodes(self, selector):
        """Find listing cards with XPath first, then regex fallback if needed."""
        xpath_candidates = [
            # class token-aware matching (no regex)
            '//div[contains(concat(" ", normalize-space(@class), " "), " listingCard ")]',
            '//div[contains(concat(" ", normalize-space(@class), " "), " listing-card ")]',
            '//div[contains(concat(" ", normalize-space(@class), " "), " propResultRow ")]',
            '//div[contains(concat(" ", normalize-space(@class), " "), " result-item ")]',
            '//div[contains(concat(" ", normalize-space(@class), " "), " property-card ")]',
            # explicit attribute/link shapes
            '//div[@data-mlsnum]',
            '//div[.//a[contains(@href, "action=dsp.info")]][.//text()[contains(., "MLS")]]',
            '//tr[.//a[contains(@href, "action=dsp.info")]]',
        ]

        regex_fallback_candidates = [
            '//div[re:test(@class, "(^|\\s)(listingCard|listing-card|propResultRow|result-item|property-card)(\\s|$)")]'
        ]

        seen_html = set()
        for xpath in xpath_candidates:
            nodes = []
            for node in selector.xpath(xpath):
                html = node.get() or ""
                if html and html not in seen_html:
                    seen_html.add(html)
                    nodes.append(node)
            if nodes:
                return nodes

        for xpath in regex_fallback_candidates:
            nodes = []
            for node in selector.xpath(xpath):
                html = node.get() or ""
                if html and html not in seen_html:
                    seen_html.add(html)
                    nodes.append(node)
            if nodes:
                return nodes

        return []

    def _parse_listing_card(self, node, county):
        """Extract listing fields from a single card node."""
        node_html = node.get() or ""
        text_lines = [t.strip() for t in node.xpath(".//text()").getall() if t.strip()]

        mls_id = (
            node.xpath(".//@data-mlsnum").get()
            or node.xpath(".//@data-mls").get()
            or self._extract_mls_from_lines(text_lines)
            or self._extract_mls_from_html(node_html)
        )

        detail_href = node.xpath('.//a[contains(@href, "dsp.info")]/@href').get()
        detail_url = None
        if detail_href:
            detail_url = (
                f"https://www.njmls.com{detail_href}"
                if detail_href.startswith("/")
                else detail_href
            )
        elif mls_id:
            detail_url = (
                f"https://www.njmls.com/listings/index.cfm"
                f"?action=dsp.info&mlsnum={mls_id}&proptype=1,2,3"
            )

        address = self._extract_address(node, text_lines)
        address = self._normalize_card_address(address, mls_id)
        city, state, postal_code = self._extract_city_state_zip(text_lines)
        list_price = self._extract_price(node, text_lines)
        beds = self._extract_bedrooms(text_lines)
        full_baths = self._extract_full_baths(text_lines)
        half_baths = self._extract_half_baths(text_lines)
        baths = self._compose_total_baths(full_baths, half_baths)
        if baths is None:
            baths = self._extract_float_pattern(text_lines, BATHS_PATTERN)
        sqft = self._extract_sqft(text_lines)
        prop_type = node.xpath(".//@data-proptype").get() or self._extract_prop_type(text_lines)
        garage = self._extract_garage(text_lines)
        garage_spaces = self._extract_int_pattern(text_lines, GARAGE_SPACES_PATTERN)
        days_on_market = self._extract_days_on_market(text_lines)

        if not mls_id and not address:
            return None

        listing = {
            "source": "njmls",
            "mls_id": mls_id,
            "detail_url": detail_url,
            "address": address,
            "city": city,
            "state": state or "NJ",
            "postal_code": postal_code,
            "county": county,
            "list_price": list_price,
            "beds": beds,
            "baths": baths,
            "full_baths": full_baths,
            "half_baths": half_baths,
            "garage": garage,
            "garage_spaces": garage_spaces,
            "sqft": sqft,
            "property_type": prop_type,
            "listing_agent": None,
            "listing_agent_phone": None,
            "listing_agent_email": None,
            "listing_office": None,
            "listing_office_phone": None,
            "listing_office_email": None,
            "listing_office_contact": None,
            "cooling": None,
            "heat_cool": None,
            "lot_description": None,
            "year_built": None,
            "parking": None,
            "exterior": None,
            "days_on_market": days_on_market,
            "tax_annual_amount": None,
            "tax_year": None,
            "status": "ACTIVE",
            "photos_count": 0,
            "first_photo_url": None,
            "photo_links": [],
        }
        card_photo_links = self._extract_photo_links_from_node(node, mls_id)
        if card_photo_links:
            listing["photo_links"] = card_photo_links
            listing["photos_count"] = len(card_photo_links)
            listing["first_photo_url"] = card_photo_links[0]
        self._sanitize_bed_bath_fields(listing, source="card")
        return listing

    def parse_listing_detail(self, response):
        listing = dict(response.meta.get("listing") or {})
        if response.status != 200:
            fallback_request = self._scrapfly_detail_request(
                listing=listing,
                original_url=listing.get("detail_url") or response.url,
                blocked_status=response.status,
            )
            if isinstance(fallback_request, scrapy.Request):
                yield fallback_request
                return
            self.logger.warning(
                "Detail request failed mls_id=%s status=%s",
                listing.get("mls_id"),
                response.status,
            )
            listing["detail_http_status"] = response.status
            listing["detail_parse_status"] = f"non_200_{response.status}"
            yield listing
            return

        text = self._safe_response_text(response)
        if not text:
            fallback_request = self._scrapfly_detail_request(
                listing=listing,
                original_url=listing.get("detail_url") or response.url,
                blocked_status=response.status,
            )
            if isinstance(fallback_request, scrapy.Request):
                yield fallback_request
                return
            listing["detail_http_status"] = response.status
            listing["detail_parse_status"] = "empty_body"
            yield listing
            return
        parsed = self._parse_listing_detail_payload(
            listing=listing,
            text=text,
            response_url=response.url,
            status_code=response.status,
            parse_status="ok",
        )
        yield parsed

    def parse_listing_detail_scrapfly(self, response):
        listing = dict(response.meta.get("listing") or {})
        blocked_status = response.meta.get("blocked_status")
        upstream_status, upstream_text = self._extract_scrapfly_upstream_result(response)
        if upstream_status is None:
            upstream_status = blocked_status or response.status

        if upstream_status != 200:
            listing["detail_http_status"] = upstream_status
            listing["detail_parse_status"] = f"scrapfly_non_200_{upstream_status}"
            yield listing
            return
        if not upstream_text:
            listing["detail_http_status"] = upstream_status
            listing["detail_parse_status"] = "scrapfly_empty_body"
            yield listing
            return

        parsed = self._parse_listing_detail_payload(
            listing=listing,
            text=upstream_text,
            response_url=listing.get("detail_url") or response.url,
            status_code=upstream_status,
            parse_status="scrapfly_ok",
        )
        yield parsed

    def _parse_listing_detail_payload(self, listing, text, response_url, status_code, parse_status):
        listing["detail_http_status"] = status_code
        listing["detail_parse_status"] = parse_status
        detail_sel = scrapy.Selector(text=text)
        detail_lines = self._extract_clean_lines(detail_sel)
        detail_address, detail_city, detail_state, detail_postal_code = self._extract_detail_location(
            detail_sel, detail_lines
        )

        bedrooms = self._extract_labeled_int_xpath(detail_sel,"bedrooms")
        if bedrooms is None:
            bedrooms = self._extract_labeled_int(detail_lines, "bedrooms")

        full_baths = self._extract_labeled_int_xpath(detail_sel,"full baths")
        if full_baths is None:
            full_baths = self._extract_labeled_int(detail_lines, "full baths")

        half_baths = self._extract_labeled_int_xpath(detail_sel,"half baths")
        if half_baths is None:
            half_baths = self._extract_labeled_int(detail_lines, "half baths")

        garage = self._extract_labeled_value_xpath(detail_sel,"garage")
        if garage is None:
            garage = self._extract_labeled_value(detail_lines, "garage")

        cooling = self._extract_labeled_value_xpath(detail_sel,"cooling")
        if cooling is None:
            cooling = self._extract_labeled_value(detail_lines, "cooling")

        heat_cool = self._extract_labeled_value_xpath(detail_sel,"heat/cool")
        if heat_cool is None:
            heat_cool = self._extract_labeled_value(detail_lines, "heat/cool")

        lot_description = self._extract_labeled_value_xpath(detail_sel,"lot description")
        if lot_description is None:
            lot_description = self._extract_labeled_value(detail_lines, "lot description")

        year_built = self._extract_labeled_value_xpath(detail_sel,"year built")
        if year_built is None:
            year_built = self._extract_labeled_value(detail_lines, "year built")
        if year_built is None:
            year_built = self._extract_year_built(detail_lines)

        parking = self._extract_labeled_value_xpath(detail_sel,"parking")
        if parking is None:
            parking = self._extract_labeled_value(detail_lines, "parking")

        exterior = self._extract_labeled_value_xpath(detail_sel,"exterior")
        if exterior is None:
            exterior = self._extract_labeled_value(detail_lines, "exterior")
        days_on_market = self._extract_labeled_int_xpath(detail_sel, "days on market")
        if days_on_market is None:
            days_on_market = self._extract_labeled_int(detail_lines, "days on market")
        if days_on_market is None:
            days_on_market = self._extract_labeled_int_xpath(detail_sel, "dom")
        if days_on_market is None:
            days_on_market = self._extract_labeled_int(detail_lines, "dom")
        if days_on_market is None:
            days_on_market = self._extract_int_pattern(detail_lines, DAYS_ON_MARKET_PATTERN)

        tax_amount_raw = self._extract_labeled_value_xpath(detail_sel, "taxes")
        if tax_amount_raw is None:
            tax_amount_raw = self._extract_labeled_value(detail_lines, "taxes")
        if tax_amount_raw is None:
            tax_amount_raw = self._extract_labeled_value_xpath(detail_sel, "tax amount")
        if tax_amount_raw is None:
            tax_amount_raw = self._extract_labeled_value(detail_lines, "tax amount")
        tax_annual_amount = self._parse_price(tax_amount_raw)

        tax_year = self._extract_labeled_int_xpath(detail_sel, "tax year")
        if tax_year is None:
            tax_year = self._extract_labeled_int(detail_lines, "tax year")
        if tax_year is None:
            tax_year = self._extract_int_pattern(detail_lines, TAX_YEAR_CONTEXT_PATTERN)

        detail_style = self._extract_labeled_value_xpath(detail_sel, "style")
        if detail_style is None:
            detail_style = self._extract_labeled_value(detail_lines, "style")
        detail_category = self._extract_labeled_value_xpath(detail_sel, "category")
        if detail_category is None:
            detail_category = self._extract_labeled_value(detail_lines, "category")
        detail_sqft = self._extract_sqft(detail_lines)

        garage_spaces = self._extract_garage_spaces_xpath(detail_sel)
        if garage_spaces is None:
            garage_spaces = self._extract_int_pattern(detail_lines, GARAGE_PARKING_PATTERN)
        if garage_spaces is None:
            garage_spaces = self._extract_int_pattern(detail_lines, GARAGE_SPACES_PATTERN)

        if not garage and garage_spaces is not None:
            garage = str(garage_spaces)

        if bedrooms is not None:
            listing["beds"] = bedrooms
        if full_baths is not None:
            listing["full_baths"] = full_baths
        if half_baths is not None:
            listing["half_baths"] = half_baths
        if garage is not None:
            listing["garage"] = garage
        if garage_spaces is not None:
            listing["garage_spaces"] = garage_spaces
        if cooling is not None:
            listing["cooling"] = cooling
        if heat_cool is not None:
            listing["heat_cool"] = heat_cool
        if lot_description is not None:
            listing["lot_description"] = lot_description
        if year_built is not None:
            listing["year_built"] = year_built
        if parking is not None:
            listing["parking"] = parking
        if exterior is not None:
            listing["exterior"] = exterior
        if days_on_market is not None:
            listing["days_on_market"] = days_on_market
        if tax_annual_amount is not None:
            listing["tax_annual_amount"] = tax_annual_amount
        if tax_year is not None:
            listing["tax_year"] = tax_year
        if not listing.get("property_type"):
            detail_prop_type = self._coerce_property_type(detail_style, detail_category)
            if detail_prop_type:
                listing["property_type"] = detail_prop_type
        if listing.get("sqft") is None and detail_sqft is not None:
            listing["sqft"] = detail_sqft
        if detail_address is not None:
            listing["address"] = detail_address
        if detail_city is not None and not listing.get("city"):
            listing["city"] = detail_city
        if detail_state is not None and not listing.get("state"):
            listing["state"] = detail_state
        if detail_postal_code is not None and not listing.get("postal_code"):
            listing["postal_code"] = detail_postal_code

        property_remarks = self._extract_property_remarks(detail_sel, detail_lines)
        if property_remarks is not None:
            listing["property_remarks"] = property_remarks

        detail_photo_links = self._extract_photo_links_from_detail_page(
            detail_sel,
            text,
            listing.get("mls_id"),
            response_url,
        )
        existing_links = listing.get("photo_links") or []
        if detail_photo_links and len(detail_photo_links) >= len(existing_links):
            listing["photo_links"] = detail_photo_links
            listing["photos_count"] = len(detail_photo_links)
            listing["first_photo_url"] = detail_photo_links[0]
        elif existing_links:
            listing["photos_count"] = len(existing_links)
            listing["first_photo_url"] = existing_links[0]

        listing_agent = self._extract_listing_agent(detail_sel, detail_lines)
        if listing_agent is not None:
            listing["listing_agent"] = listing_agent

        listing_office = self._extract_listing_office(detail_sel, detail_lines)
        if listing_office is not None:
            listing["listing_office"] = listing_office

        agent_phone, agent_email = self._extract_contact_details(
            detail_sel,
            detail_lines,
            labels=("listing agent", "list agent", "agent"),
        )
        office_phone, office_email = self._extract_contact_details(
            detail_sel,
            detail_lines,
            labels=("listing office", "list office", "office"),
        )

        if agent_phone:
            listing["listing_agent_phone"] = agent_phone
        if agent_email:
            listing["listing_agent_email"] = agent_email
        if office_phone:
            listing["listing_office_phone"] = office_phone
        if office_email:
            listing["listing_office_email"] = office_email
        office_contact = self._compose_contact(office_phone, office_email)
        if office_contact:
            listing["listing_office_contact"] = office_contact

        baths = self._compose_total_baths(full_baths, half_baths)
        if baths is not None:
            listing["baths"] = baths
        elif full_baths is not None:
            listing["baths"] = float(full_baths)

        self._sanitize_bed_bath_fields(listing, source="detail")
        return listing

    # ── extraction helpers ────────────────────────────────────────────────────

    def _extract_labeled_value_xpath(self, sel, label):
        label_key = label.lower()
        label_expr = (
            f'(//text()[starts-with(translate(normalize-space(.), "{UPPERCASE}", "{LOWERCASE}"), "{label_key}")])[1]'
        )

        raw = sel.xpath(f"normalize-space({label_expr})").get()
        raw = " ".join((raw or "").split()).strip()
        if raw:
            value = raw.split(":", 1)[1].strip() if ":" in raw else ""
            if value:
                return value

        next_value = sel.xpath(
            f"normalize-space({label_expr}/following::text()[normalize-space()][1])"
        ).get()
        next_value = " ".join((next_value or "").split()).strip()
        if next_value and ":" not in next_value:
            return next_value
        return None

    def _extract_labeled_int_xpath(self, sel, label):
        raw = self._extract_labeled_value_xpath(sel, label)
        if raw is None:
            return None
        match = re.search(r"\d+", raw)
        if not match:
            return None
        try:
            return int(match.group(0))
        except (TypeError, ValueError):
            return None

    def _extract_garage_spaces_xpath(self, sel):
        candidates = sel.xpath(
            f'//text()[contains(translate(., "{UPPERCASE}", "{LOWERCASE}"), "parking spaces in the garage") or contains(translate(., "{UPPERCASE}", "{LOWERCASE}"), "car garage")]'
        ).getall()
        if not candidates:
            return None
        return self._extract_int_pattern(candidates, GARAGE_PARKING_PATTERN) or self._extract_int_pattern(
            candidates, GARAGE_SPACES_PATTERN
        )

    @staticmethod
    def _extract_year_built(text_lines):
        for line in text_lines:
            m = YEAR_BUILT_PATTERN.search(line)
            if m:
                return m.group(0)
        return None

    def _extract_bedrooms(self, text_lines):
        labeled = self._extract_labeled_int(text_lines, "bedrooms")
        if labeled is not None:
            return labeled
        return self._extract_int_pattern(text_lines, BEDS_PATTERN)

    def _extract_full_baths(self, text_lines):
        labeled = self._extract_labeled_int(text_lines, "full baths")
        if labeled is not None:
            return labeled
        return self._extract_int_pattern(text_lines, FULL_BATHS_PATTERN)

    def _extract_half_baths(self, text_lines):
        labeled = self._extract_labeled_int(text_lines, "half baths")
        if labeled is not None:
            return labeled
        return self._extract_int_pattern(text_lines, HALF_BATHS_PATTERN)

    def _extract_garage(self, text_lines):
        labeled = self._extract_labeled_value(text_lines, "garage")
        if labeled is not None:
            return labeled

        spaces = self._extract_int_pattern(text_lines, GARAGE_PARKING_PATTERN)
        if spaces is not None:
            return str(spaces)
        return None

    def _extract_days_on_market(self, text_lines):
        labeled = self._extract_labeled_int(text_lines, "days on market")
        if labeled is not None:
            return labeled
        labeled_dom = self._extract_labeled_int(text_lines, "dom")
        if labeled_dom is not None:
            return labeled_dom
        return self._extract_int_pattern(text_lines, DAYS_ON_MARKET_PATTERN)

    @staticmethod
    def _compose_total_baths(full_baths, half_baths):
        if full_baths is None and half_baths is None:
            return None
        return float((full_baths or 0) + (half_baths or 0) * 0.5)

    def _sanitize_bed_bath_fields(self, listing, source):
        beds = listing.get("beds")
        baths = listing.get("baths")
        full_baths = listing.get("full_baths")
        half_baths = listing.get("half_baths")
        list_price = listing.get("list_price")

        # Be conservative with half-bath validation to avoid rejecting valid homes.
        # Only discard clearly implausible spreads.
        if (
            full_baths is not None
            and half_baths is not None
            and half_baths > full_baths + 3
        ):
            self.logger.debug(
                "Discarding invalid half_baths=%s full_baths=%s mls_id=%s source=%s",
                half_baths,
                full_baths,
                listing.get("mls_id"),
                source,
            )
            half_baths = None
            baths = float(full_baths)

        # Ignore half baths for validation; use full baths when available.
        baths_for_validation = full_baths if full_baths is not None else baths

        if baths_for_validation is not None and baths_for_validation >= 16:
            self.logger.debug(
                "Discarding outlier baths=%s mls_id=%s source=%s",
                baths_for_validation,
                listing.get("mls_id"),
                source,
            )
            baths = None

        if (
            baths_for_validation is not None
            and list_price is not None
            and list_price < 3_000_000
            and baths_for_validation >= 10
        ):
            self.logger.debug(
                "Discarding high baths=%s for sub-$3M listing mls_id=%s source=%s",
                baths_for_validation,
                listing.get("mls_id"),
                source,
            )
            baths = None

        if beds is not None and (beds <= 0 or beds >= 20):
            self.logger.debug(
                "Discarding outlier beds=%s mls_id=%s source=%s",
                beds,
                listing.get("mls_id"),
                source,
            )
            beds = None

        if (
            beds is not None
            and baths_for_validation is not None
            and list_price is not None
            and list_price < 1_500_000
        ):
            # For sub-$1.5M homes, bath count above beds by > 3 is usually bad parse data.
            if baths_for_validation > beds + 3:
                self.logger.debug(
                    "Discarding bath/bed mismatch baths=%s beds=%s mls_id=%s source=%s",
                    baths_for_validation,
                    beds,
                    listing.get("mls_id"),
                    source,
                )
                baths = None

        listing["beds"] = beds
        listing["baths"] = baths
        listing["half_baths"] = half_baths

    @staticmethod
    def _extract_labeled_value(text_lines, label):
        prefix = f"{label.lower()}:"
        for idx, line in enumerate(text_lines):
            normalized = " ".join(line.split()).strip()
            lowered = normalized.lower()
            if lowered.startswith(prefix):
                value = normalized.split(":", 1)[1].strip()
                if value:
                    return value
                if idx + 1 < len(text_lines):
                    next_value = " ".join(text_lines[idx + 1].split()).strip()
                    if next_value and ":" not in next_value:
                        return next_value
                    return None
                return None
        return None

    @staticmethod
    def _extract_labeled_int(text_lines, label):
        raw = NjmlsSpider._extract_labeled_value(text_lines, label)
        if raw is None:
            return None
        match = re.search(r"\d+", raw)
        if not match:
            return None
        try:
            return int(match.group(0))
        except (TypeError, ValueError):
            return None

    def _extract_address(self, node, text_lines):
        candidates = [
            './/span[contains(@class, "address")]//text()',
            './/div[contains(@class, "address")]//text()',
            './/h3//text()',
            './/h4//text()',
            './/a[contains(@href, "dsp.info")]//text()',
        ]
        for xpath in candidates:
            parts = node.xpath(xpath).getall()
            value = " ".join(p.strip() for p in parts if p.strip())
            if value and re.search(r"\d", value):
                return value
        return self._extract_address_from_lines(text_lines)

    def _extract_address_from_lines(self, text_lines):
        for line in text_lines:
            line = " ".join(line.split())
            if not line or "MLS" in line or "$" in line:
                continue
            if CITY_STATE_ZIP_PATTERN.match(line):
                continue
            if re.search(r"\d", line) and len(line) > 5:
                return line
        return None

    @staticmethod
    def _normalize_card_address(address, mls_id):
        value = NjmlsSpider._clean_str(address)
        if not value:
            return None
        collapsed = re.sub(r"\s+", " ", value).strip()
        lowered = collapsed.lower()
        if "mls number" in lowered:
            return None

        digits_only = re.sub(r"\D", "", collapsed)
        if digits_only and digits_only == str(mls_id or ""):
            return None
        # Guard against numeric-only artifacts (for example MLS ID from link text).
        if digits_only and len(digits_only) == len(collapsed):
            return None
        # A plausible street address should include at least one alphabetic character.
        if not re.search(r"[A-Za-z]", collapsed):
            return None
        return collapsed

    def _extract_city_state_zip(self, text_lines):
        for line in text_lines:
            m = CITY_STATE_ZIP_PATTERN.match(line.strip())
            if m:
                return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        return None, None, None

    def _extract_detail_location(self, selector, text_lines):
        address = self._extract_labeled_value_xpath(selector, "address")
        if address is None:
            address = self._extract_labeled_value(text_lines, "address")

        city = None
        state = None
        postal_code = None

        # Prefer explicit "Street, City, ST [ZIP]" lines from detail page text.
        for raw_line in text_lines or []:
            line = " ".join((raw_line or "").split()).strip().lstrip(">").strip()
            if not line or line.lower().startswith("mls number"):
                continue
            m = ADDRESS_CITY_STATE_OPTIONAL_ZIP_PATTERN.match(line)
            if not m:
                continue
            street, city_value, state_value, zip_value = m.groups()
            street = " ".join((street or "").split()).strip()
            city_value = " ".join((city_value or "").split()).strip()
            state_value = (state_value or "").strip()
            zip_value = (zip_value or "").strip() or None

            if street and re.search(r"\d", street):
                if address is None:
                    address = street
                city = city_value or city
                state = state_value or state
                postal_code = zip_value or postal_code
                break

        # Fallback to title/og:title when city/state were not found in clean lines.
        if address is None or city is None or state is None:
            title_candidates = [
                selector.xpath('normalize-space(//meta[@property="og:title"]/@content)').get(),
                selector.xpath("normalize-space(//title)").get(),
            ]
            for title in title_candidates:
                parsed = self._extract_location_from_detail_title(title)
                if not parsed:
                    continue
                t_address, t_city, t_state, t_zip = parsed
                address = address or t_address
                city = city or t_city
                state = state or t_state
                postal_code = postal_code or t_zip
                break

        return (
            self._clean_str(address),
            self._clean_str(city),
            self._clean_str(state),
            self._clean_str(postal_code),
        )

    @staticmethod
    def _extract_location_from_detail_title(title):
        text = " ".join((title or "").split()).strip()
        if not text:
            return None
        matches = DETAIL_TITLE_ADDRESS_PATTERN.findall(text)
        if not matches:
            return None
        street, city, state, postal = matches[-1]
        return (
            street.strip() or None,
            city.strip() or None,
            state.strip() or None,
            (postal or "").strip() or None,
        )

    @staticmethod
    def _clean_str(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    def _extract_price(self, node, text_lines):
        candidates = [
            './/span[contains(@class, "price")]//text()',
            './/div[contains(@class, "price")]//text()',
            './/strong[contains(text(), "$")]//text()',
        ]
        for xpath in candidates:
            raw = " ".join(node.xpath(xpath).getall()).strip()
            price = self._parse_price(raw)
            if price:
                return price
        for line in text_lines:
            price = self._parse_price(line)
            if price:
                return price
        return None

    @staticmethod
    def _parse_price(text):
        m = PRICE_PATTERN.search(text or "")
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_mls_from_lines(text_lines):
        for line in text_lines:
            if "MLS" in line.upper():
                m = MLS_PATTERN.search(line)
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _extract_mls_from_html(html):
        m = MLS_PATTERN.search(html or "")
        return m.group(1) if m else None

    @staticmethod
    def _extract_int_pattern(text_lines, pattern):
        for line in text_lines:
            m = pattern.search(line)
            if m:
                try:
                    return int(m.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_float_pattern(text_lines, pattern):
        for line in text_lines:
            m = pattern.search(line)
            if m:
                try:
                    return float(m.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_sqft(text_lines):
        for line in text_lines:
            m = SQFT_PATTERN.search(line)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_prop_type(text_lines):
        keywords = {
            "Single Family": ["single family", "sfr"],
            "Condo": ["condo", "condominium"],
            "Townhouse": ["townhouse", "townhome"],
            "Multi Family": ["multi family", "multifamily"],
            "Land": ["land", "lot"],
            "Rental": ["rental"],
        }
        for line in text_lines:
            lower = line.lower()
            for prop_type, terms in keywords.items():
                if any(t in lower for t in terms):
                    return prop_type
        return None

    def _coerce_property_type(self, style, category):
        values = [v for v in (style, category) if v]
        if not values:
            return None
        mapped = self._extract_prop_type(values)
        if mapped:
            return mapped
        # Keep the style/category value rather than dropping to None.
        return self._clean_str(values[0])

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except Exception:
            try:
                return response.body.decode("utf-8", errors="replace")
            except Exception:
                return ""

    def _scrapfly_detail_request(self, listing, original_url, blocked_status):
        if not (self.scrapfly_asp_enabled and self.scrapfly_api_key and original_url):
            return listing
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
            "NJMLS detail blocked status=%s mls_id=%s; retrying via Scrapfly ASP",
            blocked_status,
            listing.get("mls_id"),
        )
        self.logger.debug(
            "NJMLS detail retrying with Scrapfly ASP: status=%s mls_id=%s url=%s",
            blocked_status,
            listing.get("mls_id"),
            original_url,
        )
        return scrapy.Request(
            scrapfly_url,
            callback=self.parse_listing_detail_scrapfly,
            headers={"accept": "application/json"},
            meta={
                "listing": dict(listing or {}),
                "blocked_status": blocked_status,
            },
            dont_filter=True,
        )

    @staticmethod
    def _extract_scrapfly_upstream_result(response):
        text = NjmlsSpider._safe_response_text(response)
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

    def _extract_photo_links_from_node(self, node, mls_id):
        candidates = []
        attr_xpaths = [
            ".//img/@src",
            ".//img/@data-src",
            ".//img/@data-original",
            ".//img/@data-lazy",
            ".//*[@style]/@style",
        ]
        for xpath in attr_xpaths:
            candidates.extend(node.xpath(xpath).getall())
        node_html = node.get() or ""
        candidates.extend(PHOTO_URL_PATTERN.findall(node_html))
        return self._normalize_and_filter_photo_links(
            raw_values=candidates,
            mls_id=mls_id,
            base_url="https://www.njmls.com/",
        )

    def _extract_photo_links_from_detail_page(self, selector, page_text, mls_id, page_url):
        candidates = []
        attr_xpaths = [
            "//img/@src",
            "//img/@data-src",
            "//img/@data-original",
            "//img/@data-lazy",
            "//*[@style]/@style",
        ]
        for xpath in attr_xpaths:
            candidates.extend(selector.xpath(xpath).getall())
        candidates.extend(PHOTO_URL_PATTERN.findall(page_text or ""))
        return self._normalize_and_filter_photo_links(
            raw_values=candidates,
            mls_id=mls_id,
            base_url=page_url,
        )

    def _normalize_and_filter_photo_links(self, raw_values, mls_id, base_url):
        links = []
        seen = set()
        for raw in raw_values or []:
            for candidate in self._expand_image_candidates(raw, base_url):
                if not self._is_property_photo_url(candidate, mls_id):
                    continue
                if candidate in seen:
                    continue
                seen.add(candidate)
                links.append(candidate)
        return links

    @staticmethod
    def _expand_image_candidates(raw_value, base_url):
        value = (raw_value or "").strip()
        if not value:
            return []
        candidates = []
        matches = STYLE_URL_PATTERN.findall(value)
        if matches:
            candidates.extend(m[1] for m in matches if m and m[1])
        else:
            candidates.append(value)

        normalized = []
        for candidate in candidates:
            c = candidate.strip().strip('"').strip("'")
            if not c:
                continue
            if c.startswith("//"):
                c = f"https:{c}"
            elif c.startswith("http://") or c.startswith("https://"):
                pass
            else:
                c = urljoin(base_url, c)
            normalized.append(c)
        return normalized

    @staticmethod
    def _is_property_photo_url(url, mls_id):
        lowered = (url or "").lower()
        if not lowered:
            return False
        if "njmls.com/assets/" in lowered:
            return False
        if not re.search(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", lowered):
            return False
        blocked_tokens = (
            "logo",
            "icon",
            "favicon",
            "equalhousing",
            "facebook",
            "instagram",
            "twitter",
            "linkedin",
            "youtube",
            "avatar",
            "sprite",
            "banner",
            "pixel",
        )
        if any(token in lowered for token in blocked_tokens):
            return False
        if mls_id and str(mls_id) in lowered:
            return True
        preferred_tokens = ("listing", "listings", "property", "photo", "photos", "mls", "media")
        return any(token in lowered for token in preferred_tokens)

    def _extract_property_remarks(self, selector, text_lines):
        # New NJMLS detail layout: section heading + following paragraph.
        heading_nodes = selector.xpath(
            f'//h4[contains(translate(normalize-space(.), "{UPPERCASE}", "{LOWERCASE}"), "property remarks")]'
        )
        for heading in heading_nodes[:4]:
            candidates = [
                heading.xpath(
                    'normalize-space(string(ancestor::div[contains(@class,"col-12")][1]//p[normalize-space()][1]))'
                ).get(),
                heading.xpath('normalize-space(string(following::p[normalize-space()][1]))').get(),
            ]
            for raw in candidates:
                remarks = " ".join((raw or "").split()).strip()
                if remarks and len(remarks) > 20 and "property remarks" not in remarks.lower():
                    return remarks

        xpath_candidates = [
            '//div[contains(@class,"remarks")]//text()',
            '//section[contains(@class,"remarks")]//text()',
            '//p[contains(@class,"remarks")]//text()',
            '//div[contains(@id,"remarks")]//text()',
            '//span[contains(@class,"remarks")]//text()',
        ]
        for xpath in xpath_candidates:
            parts = selector.xpath(xpath).getall()
            remarks = " ".join(p.strip() for p in parts if p and p.strip())
            remarks = remarks.strip()
            if remarks and len(remarks) > 20:
                return remarks

        remarks = self._extract_labeled_value_xpath(selector, "remarks")
        if remarks:
            return remarks
        remarks = self._extract_labeled_value(text_lines, "remarks")
        if remarks:
            return remarks
        remarks = self._extract_labeled_value(text_lines, "property remarks")
        if remarks:
            return remarks

        # Label/value can also be split across adjacent clean text lines.
        for idx, line in enumerate(text_lines or []):
            lowered = " ".join((line or "").split()).strip().lower()
            if lowered != "property remarks":
                continue
            for nxt in text_lines[idx + 1 : idx + 8]:
                candidate = " ".join((nxt or "").split()).strip()
                if not candidate:
                    continue
                if candidate.lower() in {"property features", "presented by"}:
                    break
                if len(candidate) > 20:
                    return candidate
        return None

    def _extract_listing_agent(self, selector, text_lines):
        section_value = self._extract_presented_by_name(selector, section_label="listing agent")
        if section_value:
            return section_value
        for label in ("listing agent", "agent", "list agent"):
            value = self._extract_labeled_value_xpath(selector, label)
            if value and not self._is_contact_cta_or_label(value):
                return value
            value = self._extract_labeled_value(text_lines, label)
            if value and not self._is_contact_cta_or_label(value):
                return value
        return None

    def _extract_listing_office(self, selector, text_lines):
        section_value = self._extract_presented_by_name(selector, section_label="listing office")
        if section_value:
            return section_value
        for label in ("listing office", "office", "list office"):
            value = self._extract_labeled_value_xpath(selector, label)
            if value and not self._is_contact_cta_or_label(value):
                return value
            value = self._extract_labeled_value(text_lines, label)
            if value and not self._is_contact_cta_or_label(value):
                return value
        return None

    def _extract_contact_details(self, selector, text_lines, labels):
        section_label = "listing office" if any("office" in (l or "").lower() for l in labels) else "listing agent"
        section_node = self._presented_by_section(selector, section_label=section_label)
        section_phone, section_email = self._extract_presented_by_contact(selector, section_label=section_label)

        # If the agent block exists but contains no contact details, do not
        # backfill from page-level fallbacks (they often belong to office block).
        if (
            section_label == "listing agent"
            and section_node is not None
            and not section_phone
            and not section_email
        ):
            return None, None

        line_contexts = self._collect_contact_line_contexts(text_lines, labels)
        contexts = list(line_contexts)
        contexts.extend(self._collect_contact_contexts(selector, labels))
        for label in labels:
            value = self._extract_labeled_value_xpath(selector, label)
            if value:
                contexts.append(value)
            value = self._extract_labeled_value(text_lines, label)
            if value:
                contexts.append(value)

        phone = section_phone or self._first_phone(line_contexts) or self._first_phone(contexts)
        email = section_email or self._first_email(line_contexts) or self._first_email(contexts)

        # Conservative fallback: if section-level parsing misses contacts,
        # use page-level tel/mailto links.
        if not phone:
            phone = self._first_phone(
                selector.xpath(
                    '//a[starts-with(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "tel:")]/@href'
                ).getall()
            )
        if not email:
            email = self._first_email(
                selector.xpath(
                    '//a[starts-with(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "mailto:")]/@href'
                ).getall()
            )
        # Avoid leaking office phone into agent phone when no agent number exists.
        if not phone and section_label == "listing office":
            phone = self._first_phone(text_lines)
        if not email:
            email = self._first_email(text_lines)
        return phone, email

    def _extract_presented_by_name(self, selector, section_label):
        section = self._presented_by_section(selector, section_label=section_label)
        if section is None:
            return None
        candidates = section.xpath('.//p[contains(@class,"nj-addressCont")]/text()').getall()
        for raw in candidates:
            value = " ".join((raw or "").split()).strip()
            if not value:
                continue
            if self._is_contact_cta_or_label(value):
                continue
            # Prefer plausible person/office strings over numeric-only fragments.
            if not re.search(r"[A-Za-z]", value):
                continue
            return value
        return None

    def _extract_presented_by_contact(self, selector, section_label):
        section = self._presented_by_section(selector, section_label=section_label)
        if section is None:
            return None, None
        chunks = section.xpath(
            './/text() | .//a[starts-with(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "tel:") or starts-with(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "mailto:")]/@href'
        ).getall()
        phone = self._first_phone(chunks)
        email = self._first_email(chunks)
        return phone, email

    def _presented_by_section(self, selector, section_label):
        label_key = (section_label or "").strip().lower()
        blocks = selector.xpath(
            '//ul[contains(@class,"nj-presentedBy")][1]//div[contains(@class,"nj-realtorResults")]'
        )
        for block in blocks:
            heading = block.xpath(
                f'normalize-space(string(.//span[contains(@class,"h5")][contains(translate(normalize-space(.), "{UPPERCASE}", "{LOWERCASE}"), "{label_key}")][1]))'
            ).get()
            if heading:
                return block
        return None

    @staticmethod
    def _is_contact_cta_or_label(value):
        lowered = " ".join((value or "").split()).strip().lower()
        if not lowered:
            return True
        blocked = {
            "listing agent",
            "listing office",
            "agent",
            "office",
            "view my active listings",
            "view my sold listings",
            "all office listings",
            "request more info",
        }
        return lowered in blocked

    @staticmethod
    def _compose_contact(phone, email):
        phone = (phone or "").strip()
        email = (email or "").strip()
        if phone and email:
            return f"{phone} | {email}"
        if phone:
            return phone
        if email:
            return email
        return None

    def _collect_contact_contexts(self, selector, labels):
        contexts = []
        seen = set()
        container_xpath = (
            "ancestor::li[1] | ancestor::tr[1] | ancestor::td[1] | "
            "ancestor::section[1] | ancestor::div[1] | parent::*[1]"
        )

        for label in labels:
            label_key = label.lower()
            nodes = selector.xpath(
                f'//*[contains(translate(normalize-space(string(.)), "{UPPERCASE}", "{LOWERCASE}"), "{label_key}")]'
            )
            for node in nodes[:8]:
                snippets = node.xpath(
                    f"normalize-space(string({container_xpath}))"
                ).getall()
                snippets.extend(
                    node.xpath(
                        "normalize-space(string(.))"
                    ).getall()
                )
                snippets.extend(
                    node.xpath(
                        "normalize-space(string(following-sibling::*[1]))"
                    ).getall()
                )
                snippets.extend(
                    node.xpath(
                        'normalize-space(string(following::text()[normalize-space()][1]))'
                    ).getall()
                )
                snippets.extend(
                    node.xpath(
                        f'({container_xpath})//a[starts-with(translate(@href, "{UPPERCASE}", "{LOWERCASE}"), "mailto:") or starts-with(translate(@href, "{UPPERCASE}", "{LOWERCASE}"), "tel:")]/@href'
                    ).getall()
                )
                for raw in snippets:
                    text = " ".join((raw or "").split()).strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    contexts.append(text)
        return contexts

    @staticmethod
    def _collect_contact_line_contexts(text_lines, labels):
        contexts = []
        seen = set()
        lowered_labels = tuple((label or "").strip().lower() for label in labels if label)
        normalized_lines = [" ".join((line or "").split()).strip() for line in (text_lines or [])]

        for idx, line in enumerate(normalized_lines):
            if not line:
                continue
            lowered = line.lower()
            if not any(label in lowered for label in lowered_labels):
                continue
            window_end = min(len(normalized_lines), idx + 8)
            for candidate in normalized_lines[idx:window_end]:
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                contexts.append(candidate)
        return contexts

    @staticmethod
    def _first_email(chunks):
        for chunk in chunks or []:
            if not chunk:
                continue
            text = (chunk or "").strip()
            if text.lower().startswith("mailto:"):
                text = text.split(":", 1)[1].split("?", 1)[0]
            m = EMAIL_PATTERN.search(text)
            if m:
                return m.group(0).strip().lower()
        return None

    @staticmethod
    def _normalize_phone(phone):
        digits = re.sub(r"\D", "", phone or "")
        # Keep the core US number and ignore trailing extension digits.
        if len(digits) >= 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) < 10:
            return None
        digits = digits[:10]
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"

    def _first_phone(self, chunks):
        for chunk in chunks or []:
            if not chunk:
                continue
            text = (chunk or "").strip()
            if text.lower().startswith("tel:"):
                text = text.split(":", 1)[1]
            m = PHONE_PATTERN.search(text)
            if not m:
                continue
            normalized = self._normalize_phone(m.group(0))
            if normalized:
                return normalized
        return None

    @staticmethod
    def _extract_mls_from_url(url):
        m = MLS_IN_URL_PATTERN.search(url or "")
        return m.group(1) if m else None

    @staticmethod
    def _extract_clean_lines(selector):
        lines = []
        for raw in selector.xpath("//text()[not(ancestor::script) and not(ancestor::style)]").getall():
            text = " ".join((raw or "").split()).strip()
            if not text:
                continue
            lines.append(text)
        return lines
