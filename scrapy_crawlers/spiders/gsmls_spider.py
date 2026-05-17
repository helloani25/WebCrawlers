import json
import re
from urllib import error, request

import scrapy
from scrapy.exceptions import CloseSpider

from spiders.env_config import build_proxy_url, get_env

COUNTY_SEARCH_URL = "https://www2.gsmls.com/publicsite/getcountysearch.do?method=getcountysearch"
COMMUNITY_SEARCH_URL = "https://www2.gsmls.com/publicsite/getcommsearch.do?method=getcommsearch"
PROPERTY_SEARCH_URL = "https://www2.gsmls.com/publicsite/getpropertysearch.do?method=getpropertysearch"
PROPERTY_DETAILS_URL = "https://www2.gsmls.com/publicsite/getpropertydetails.do?method=getpropertydetails"

OVER_LIMIT_PATTERN = re.compile(r"var count = '(\d+)'")
DISPLAY_COUNT_PATTERN = re.compile(r"Displaying\s+(\d+)\s+")
MLS_PATTERN = re.compile(r"MLS#\s*(\d+)")
CITY_STATE_ZIP_PATTERN = re.compile(r"^(.*?),\s*([A-Z]{2})\s+([0-9-]+)$")
OPEN_MAP_PATTERN = re.compile(
    r'openmapfromadd\("([^"]*)","([^"]*)",\s*"([^"]*)","([^"]*)"\)'
)
LAT_INPUT_PATTERN = re.compile(r'name="latitude"\s+value="([^"]+)"')
LON_INPUT_PATTERN = re.compile(r'name="longitude"\s+value="([^"]+)"')
STATUS_TEXT_PATTERN = re.compile(r"\b(ACTIVE|UNDER CONTRACT|PENDING|CLOSED|EXPIRED|WITHDRAWN)\b", re.I)
PHONE_PATTERN = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
NON_STYLE_VALUES = {
    "see remarks",
    "see remark",
    "remarks",
    "none",
    "n/a",
    "na",
    "-",
}
FAILED_CARD_SNIPPET_LIMIT = 400
FAILED_CARD_HTML_LIMIT = 5000
LLM_REPAIR_PROMPT_SNIPPET_LIMIT = 1500

# These are valid values from the min/max list price controls.
# They are used to split searches that exceed GSMLS's 250-record cap.
PRICE_SPLIT_POINTS = [
    0,
    20000,
    30000,
    40000,
    50000,
    75000,
    100000,
    125000,
    150000,
    175000,
    200000,
    225000,
    250000,
    275000,
    300000,
    325000,
    350000,
    400000,
    450000,
    500000,
    550000,
    600000,
    650000,
    700000,
    750000,
    800000,
    850000,
    900000,
    1000000,
    1100000,
    1200000,
    1300000,
    1400000,
    1500000,
    1600000,
    1700000,
    1800000,
    1900000,
    2000000,
]


class GsmlsSpider(scrapy.Spider):
    name = "gsmls"
    allowed_domains = ["www2.gsmls.com"]
    handle_httpstatus_list = [401, 403, 405, 429]

    custom_settings = {
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "dnt": "1",
            "priority": "u=1, i",
            "referer": "https://www2.gsmls.com/publicsite/index.jsp",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        },
        "COOKIES_ENABLED": True,
        "DOWNLOAD_DELAY": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "CURL_IMPERSONATE": "chrome110",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {
            "1",
            "true",
            "yes",
        }
        self.proxy_url = None if disable_proxy else build_proxy_url()
        self.seen_mls_ids = set()
        self.max_towns = self._safe_int(kwargs.get("max_towns"), get_env("GSMLS_MAX_TOWNS"))
        self.max_counties = self._safe_int(
            kwargs.get("max_counties"), get_env("GSMLS_MAX_COUNTIES")
        )
        self.drift_min_parse_ratio = self._safe_float(
            kwargs.get("drift_min_parse_ratio"),
            get_env("GSMLS_DRIFT_MIN_PARSE_RATIO", default="0.8"),
        )
        self.drift_fail_fast = self._is_truthy(
            kwargs.get("drift_fail_fast"),
            get_env("GSMLS_DRIFT_FAIL_FAST", default="0"),
        )
        self.llm_repair_enabled = self._is_truthy(
            kwargs.get("llm_repair"),
            get_env("GSMLS_LLM_REPAIR_ENABLED", default="0"),
        )
        self.llm_repair_model = (
            kwargs.get("llm_repair_model")
            or get_env("GSMLS_LLM_REPAIR_MODEL", "OLLAMA_MODEL", default="llama3:8b")
        )
        self.llm_repair_url = (
            kwargs.get("llm_repair_url")
            or get_env("GSMLS_LLM_REPAIR_URL", "OLLAMA_URL", default="http://127.0.0.1:11434")
        ).rstrip("/")
        self.llm_repair_timeout = self._safe_int(
            kwargs.get("llm_repair_timeout"),
            get_env("GSMLS_LLM_REPAIR_TIMEOUT", default="20"),
        ) or 20
        self.follow_detail = not self._is_truthy(
            kwargs.get("skip_detail"),
            get_env("GSMLS_SKIP_DETAIL", default="0"),
        )
        self.drift_events = 0
        self.llm_repair_attempts = 0
        self.llm_repair_successes = 0

    @staticmethod
    def _safe_int(value, fallback=None):
        candidate = value if value not in (None, "") else fallback
        try:
            return int(candidate) if candidate not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value, fallback=None):
        candidate = value if value not in (None, "") else fallback
        try:
            return float(candidate) if candidate not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_truthy(*values):
        for value in values:
            if value is None:
                continue
            normalized = str(value).strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return False

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    async def start(self):
        if self.proxy_url:
            self.logger.info("Using DataImpulse rotating proxy for GSMLS requests")
        else:
            self.logger.warning(
                "DataImpulse proxy env vars are not fully configured; running without a proxy"
            )
        self.logger.info(
            "GSMLS drift monitor min_parse_ratio=%s fail_fast=%s llm_repair=%s model=%s",
            self.drift_min_parse_ratio,
            self.drift_fail_fast,
            self.llm_repair_enabled,
            self.llm_repair_model if self.llm_repair_enabled else "disabled",
        )
        yield scrapy.Request(
            COUNTY_SEARCH_URL,
            callback=self.parse_county_step,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def parse_county_step(self, response):
        if response.status != 200:
            self.logger.error("County step failed with status=%s", response.status)
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.error("County step returned empty/non-text response")
            return
        selector = scrapy.Selector(text=response_text)
        options = selector.xpath('//select[@id="countycode"]/option[@value]')
        county_values = []
        for opt in options:
            county_code = (opt.xpath("./@value").get() or "").strip()
            county_name = " ".join(opt.xpath(".//text()").getall()).strip()
            if not county_code:
                continue
            county_values.append((county_code, county_name))

        if self.max_counties:
            county_values = county_values[: self.max_counties]

        self.logger.info("Found %d GSMLS counties", len(county_values))
        for county_code, county_name in county_values:
            yield scrapy.Request(
                f"{COMMUNITY_SEARCH_URL}&county={county_code}",
                callback=self.parse_town_step,
                meta={
                    "county_code": county_code,
                    "county_name": county_name,
                    **self._proxy_meta(),
                },
                dont_filter=True,
            )

    def parse_town_step(self, response):
        county_code = response.meta["county_code"]
        county_name = response.meta["county_name"]
        if response.status != 200:
            self.logger.warning(
                "Town step failed county=%s(%s) status=%s",
                county_name,
                county_code,
                response.status,
            )
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.warning("Town step returned empty/non-text county=%s", county_name)
            return
        selector = scrapy.Selector(text=response_text)

        town_options = []
        for opt in selector.xpath('//select[@id="town"]/option[@value]'):
            town_code = (opt.xpath("./@value").get() or "").strip()
            town_name = " ".join(opt.xpath(".//text()").getall()).strip()
            if not town_code or town_code.upper() == "ALL":
                continue
            if not town_code.isdigit():
                continue
            town_options.append((town_code, town_name))

        if self.max_towns:
            town_options = town_options[: self.max_towns]

        self.logger.info(
            "County %s(%s): %d towns",
            county_name,
            county_code,
            len(town_options),
        )

        for town_code, town_name in town_options:
            formdata = {
                "transactionsought": "purchase",
                "propertytype": "RES",
                "town": town_code,
                "_town": "1",
                "countycode": county_code,
                "countyname": county_name,
            }
            yield scrapy.FormRequest(
                PROPERTY_SEARCH_URL,
                formdata=formdata,
                callback=self.parse_filter_step,
                meta={
                    "county_code": county_code,
                    "county_name": county_name,
                    "town_code": town_code,
                    "town_name": town_name,
                    **self._proxy_meta(),
                },
                dont_filter=True,
            )

    def parse_filter_step(self, response):
        county_code = response.meta["county_code"]
        county_name = response.meta["county_name"]
        town_code = response.meta["town_code"]
        town_name = response.meta["town_name"]

        if response.status != 200:
            self.logger.warning(
                "Filter step failed county=%s town=%s status=%s",
                county_name,
                town_name,
                response.status,
            )
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.warning(
                "Filter step returned empty/non-text county=%s town=%s",
                county_name,
                town_name,
            )
            return
        selector = scrapy.Selector(text=response_text)

        sttowns = (selector.xpath('//input[@id="sttowns"]/@value').get() or town_code).strip()
        propertytypedesc = (
            selector.xpath('//input[@id="propertytypedesc"]/@value').get() or "Residential"
        ).strip()

        base_form = {
            "countycode": county_code,
            "countyname": county_name,
            "propertytype": "RES",
            "propertytypedesc": propertytypedesc,
            "transactionsought": "purchase",
            "sttowns": sttowns,
            "minbedrooms": "0",
            "minbaths": "0",
            "minacres": "",
            "maxacres": "",
            "lotdesc": "",
        }

        yield from self._request_results(
            base_form=base_form,
            county_code=county_code,
            county_name=county_name,
            town_code=town_code,
            town_name=town_name,
            min_price=None,
            max_price=None,
            depth=0,
        )

    def _request_results(
        self,
        base_form,
        county_code,
        county_name,
        town_code,
        town_name,
        min_price,
        max_price,
        depth,
    ):
        formdata = dict(base_form)
        formdata["minlistprice"] = "" if min_price is None else str(min_price)
        formdata["maxlistprice"] = "" if max_price is None else str(max_price)
        yield scrapy.FormRequest(
            PROPERTY_DETAILS_URL,
            formdata=formdata,
            callback=self.parse_results,
            meta={
                "base_form": base_form,
                "county_code": county_code,
                "county_name": county_name,
                "town_code": town_code,
                "town_name": town_name,
                "min_price": min_price,
                "max_price": max_price,
                "depth": depth,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def parse_results(self, response):
        county_name = response.meta["county_name"]
        county_code = response.meta["county_code"]
        town_name = response.meta["town_name"]
        town_code = response.meta["town_code"]
        min_price = response.meta["min_price"]
        max_price = response.meta["max_price"]
        depth = response.meta["depth"]

        if response.status != 200:
            self.logger.warning(
                "Results step failed county=%s town=%s status=%s",
                county_name,
                town_name,
                response.status,
            )
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            self.logger.warning(
                "Results returned empty/non-text county=%s town=%s",
                county_name,
                town_name,
            )
            return
        selector = scrapy.Selector(text=response_text)
        over_limit_count = self._extract_over_limit_count(response_text)
        if over_limit_count is not None:
            split_value = self._choose_split_value(min_price=min_price, max_price=max_price)
            if split_value is None:
                self.logger.warning(
                    "Cannot split further county=%s town=%s min=%s max=%s (over_limit=%s)",
                    county_name,
                    town_name,
                    min_price,
                    max_price,
                    over_limit_count,
                )
                return

            self.logger.info(
                "Splitting over-limit county=%s town=%s count=%s range=(%s,%s) split=%s depth=%s",
                county_name,
                town_name,
                over_limit_count,
                min_price,
                max_price,
                split_value,
                depth,
            )

            base_form = response.meta["base_form"]
            # Overlap on split_value to avoid gaps; dedupe by MLS id handles duplicates.
            yield from self._request_results(
                base_form=base_form,
                county_code=county_code,
                county_name=county_name,
                town_code=town_code,
                town_name=town_name,
                min_price=min_price,
                max_price=split_value,
                depth=depth + 1,
            )
            yield from self._request_results(
                base_form=base_form,
                county_code=county_code,
                county_name=county_name,
                town_code=town_code,
                town_name=town_name,
                min_price=split_value,
                max_price=max_price,
                depth=depth + 1,
            )
            return

        display_count = self._extract_display_count(response_text)
        parsed_count = 0
        seen_on_page = set()

        result_nodes = self._result_card_nodes(selector)
        if not result_nodes:
            self.logger.warning(
                "GSMLS results parser found no card nodes county=%s town=%s min=%s max=%s",
                county_name,
                town_name,
                min_price,
                max_price,
            )
        for node in result_nodes:
            listing = self._parse_listing_card(
                node=node,
                county_name=county_name,
                county_code=county_code,
                town_name=town_name,
                town_code=town_code,
                min_price=min_price,
                max_price=max_price,
            )
            if not listing:
                listing = self._attempt_llm_repair(
                    county_name=county_name,
                    county_code=county_code,
                    town_name=town_name,
                    town_code=town_code,
                    min_price=min_price,
                    max_price=max_price,
                    node=node,
                    reason="parser_returned_none",
                )
            if not listing:
                artifact = self._build_failed_card_artifact(
                    county_name=county_name,
                    county_code=county_code,
                    town_name=town_name,
                    town_code=town_code,
                    min_price=min_price,
                    max_price=max_price,
                    reason="parser_returned_none",
                    node=node,
                )
                self._log_failed_card(artifact)
                yield artifact
                continue

            listing, validation_errors = self._validate_listing(listing)
            if validation_errors:
                repaired_listing = self._attempt_llm_repair(
                    county_name=county_name,
                    county_code=county_code,
                    town_name=town_name,
                    town_code=town_code,
                    min_price=min_price,
                    max_price=max_price,
                    node=node,
                    reason="validation_failed:" + ",".join(validation_errors),
                )
                if repaired_listing:
                    repaired_listing, repaired_errors = self._validate_listing(repaired_listing)
                    if not repaired_errors:
                        listing = self._merge_listing_dicts(repaired_listing, listing)
                        validation_errors = []
                    else:
                        validation_errors = repaired_errors
                if validation_errors:
                    artifact = self._build_failed_card_artifact(
                        county_name=county_name,
                        county_code=county_code,
                        town_name=town_name,
                        town_code=town_code,
                        min_price=min_price,
                        max_price=max_price,
                        reason="validation_failed:" + ",".join(validation_errors),
                        node=node,
                    )
                    self._log_failed_card(artifact)
                    yield artifact
            mls_id = listing.get("mls_id")
            if not mls_id:
                continue
            if mls_id in seen_on_page:
                continue
            seen_on_page.add(mls_id)
            if mls_id in self.seen_mls_ids:
                continue

            self.seen_mls_ids.add(mls_id)
            parsed_count += 1
            if self.follow_detail and listing.get("detail_url"):
                yield scrapy.Request(
                    listing["detail_url"],
                    callback=self.parse_listing_detail,
                    meta={
                        "base_item": listing,
                        **self._proxy_meta(),
                    },
                    dont_filter=True,
                )
            else:
                yield listing

        self.logger.info(
            "GSMLS county=%s town=%s min=%s max=%s display_count=%s parsed_new=%s page_seen=%s",
            county_name,
            town_name,
            min_price,
            max_price,
            display_count,
            parsed_count,
            len(seen_on_page),
        )
        self._check_drift(
            county_name=county_name,
            town_name=town_name,
            min_price=min_price,
            max_price=max_price,
            display_count=display_count,
            page_seen=len(seen_on_page),
            parsed_count=parsed_count,
        )

    def parse_listing_detail(self, response):
        base_item = dict(response.meta.get("base_item") or {})
        base_item["detail_http_status"] = response.status
        if response.status != 200:
            base_item["detail_parse_status"] = "http_error"
            yield base_item
            return

        response_text = self._safe_response_text(response)
        if not response_text:
            base_item["detail_parse_status"] = "empty_response"
            yield base_item
            return

        selector = scrapy.Selector(text=response_text)
        field_map = self._extract_detail_field_map(selector)
        overview = self._extract_overview_text(selector)
        office_name, office_phone, agent_name = self._extract_broker_contact(selector)

        style = self._normalize_style(field_map.get("Style"))
        rooms = self._to_int(field_map.get("Rooms"))
        bedrooms = self._to_int(field_map.get("Bedrooms"))
        total_baths = self._to_float(field_map.get("Total Baths"))
        acres = self._to_float(field_map.get("Acreage"))
        basement = self._clean_string(field_map.get("Basmnt/Desc"))

        if overview:
            base_item["property_remarks"] = overview
        if style:
            base_item["style"] = style
        if rooms is not None:
            base_item["rooms"] = rooms
        if bedrooms is not None:
            base_item["beds"] = bedrooms
        if total_baths is not None:
            base_item["baths"] = total_baths
        if acres is not None:
            base_item["acres"] = acres
        if basement:
            base_item["basement"] = basement

        updates = {
            "status": self._clean_string(field_map.get("Status")),
            "county": self._clean_string(field_map.get("County")),
            "town": self._clean_string(field_map.get("Cities/Towns")),
            "lot_size": self._clean_string(field_map.get("Lot Size")),
            "age_restricted_55_plus": self._clean_string(field_map.get("55+ Age Restricted")),
            "association_fee": self._to_float(field_map.get("Association Fee")),
            "full_baths": self._to_int(field_map.get("Full Baths")),
            "half_baths": self._to_int(field_map.get("Half Baths")),
            "total_baths": self._to_float(field_map.get("Total Baths")),
            "living_room_level_dim": self._clean_string(field_map.get("Living Room Level/Dim")),
            "dining_room_level_dim": self._clean_string(field_map.get("Dining Room Level/Dim")),
            "kitchen_level_dim": self._clean_string(field_map.get("Kitchen Level/Dim")),
            "family_room_level_dim": self._clean_string(field_map.get("Family Room Level/Dim")),
            "easement_desc": self._clean_string(field_map.get("Easement/Desc")),
            "garage_desc": self._clean_string(field_map.get("Garage/Desc")),
            "yb_desc": self._clean_string(field_map.get("YB/Desc")),
            "heat_source": self._clean_string(field_map.get("Heat Source")),
            "heat_system": self._clean_string(field_map.get("Heat System")),
            "cool_system": self._clean_string(field_map.get("Cool System")),
            "water": self._clean_string(field_map.get("Water")),
            "sewer": self._clean_string(field_map.get("Sewer")),
            "utilities": self._clean_string(field_map.get("Utilities")),
            "tax_amount": self._to_int(field_map.get("Tax Amount")),
            "tax_year": self._to_int(field_map.get("Tax Year")),
            "tax_rate": self._to_float(field_map.get("Tax Rate")),
            "tax_rate_year": self._to_int(field_map.get("Tax Rate Year")),
            "land_assessment": self._to_int(field_map.get("Land Assessment")),
            "building_assessment": self._to_int(field_map.get("Building Assessment")),
            "total_assessment": self._to_int(field_map.get("Total Assessment")),
            "listing_office": office_name,
            "listing_office_phone": office_phone,
            "listing_agent": agent_name,
        }
        for key, value in updates.items():
            if value not in (None, "", []):
                base_item[key] = value

        base_item["detail_fields"] = field_map
        base_item["detail_parse_status"] = "ok"
        yield base_item

    def _parse_listing_card(
        self,
        node,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
    ):
        node_html = node.get() or ""
        text_lines = self._node_text_lines(node)

        primary_listing = self._parse_listing_card_primary(
            node=node,
            node_html=node_html,
            text_lines=text_lines,
            county_name=county_name,
            county_code=county_code,
            town_name=town_name,
            town_code=town_code,
            min_price=min_price,
            max_price=max_price,
        )
        if self._listing_has_required_fields(primary_listing):
            return primary_listing

        fallback_listing = self._parse_listing_card_fallback(
            node_html=node_html,
            text_lines=text_lines,
            county_name=county_name,
            county_code=county_code,
            town_name=town_name,
            town_code=town_code,
            min_price=min_price,
            max_price=max_price,
        )
        merged_listing = self._merge_listing_dicts(primary_listing, fallback_listing)
        if not self._listing_has_required_fields(merged_listing):
            return None
        return merged_listing

    def _parse_listing_card_primary(
        self,
        node,
        node_html,
        text_lines,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
    ):
        address = self._first_joined_text(
            node,
            [
                './/a[contains(@class, "address")]//text()',
                './/a[contains(@href, "openmapfromadd")]//text()',
                './/a[contains(@href, "moredetails")]//text()',
                './/a//text()',
            ],
        )
        href = self._first_attr(
            node,
            [
                './/a[contains(@class, "address")]/@href',
                './/a[contains(@href, "openmapfromadd")]/@href',
                './/a[contains(@href, "moredetails")]/@href',
                './/a/@href',
            ],
        )
        city_line, mls_line = self._extract_labeled_lines(node, text_lines)

        mls_from_line = None
        mls_match = MLS_PATTERN.search(mls_line or "")
        if mls_match:
            mls_from_line = mls_match.group(1)

        lat = self._first_attr(
            node,
            [
                './/input[@name="latitude"]/@value',
                './/input[contains(@id, "latitude")]/@value',
            ],
        )
        lon = self._first_attr(
            node,
            [
                './/input[@name="longitude"]/@value',
                './/input[contains(@id, "longitude")]/@value',
            ],
        )

        payload_data = self._parse_open_map_payload(href or node_html)
        if not lat:
            lat = payload_data.get("lat")
        if not lon:
            lon = payload_data.get("lon")

        city, state, postal_code = self._extract_city_state_zip(text_lines, city_line)
        status_text = self._extract_status_text(node=node, text_lines=text_lines, node_html=node_html)
        mls_id = payload_data.get("mls_id") or mls_from_line or payload_data.get("sysid")

        return {
            "source": "gsmls",
            "mls_id": mls_id,
            "sys_id": payload_data.get("sysid"),
            "detail_url": self._build_detail_url(payload_data.get("sysid") or mls_id),
            "address": (payload_data.get("address") or address or "").strip() or None,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "county": county_name,
            "county_code": county_code,
            "town": payload_data.get("town") or town_name,
            "town_code": payload_data.get("town_code") or town_code,
            "status": (status_text or payload_data.get("status") or "").strip() or None,
            "list_price": self._to_int(payload_data.get("price")),
            "style": self._normalize_style(payload_data.get("style")),
            "rooms": self._to_int(payload_data.get("rooms")),
            "beds": self._to_int(payload_data.get("beds")),
            "baths": self._to_float(payload_data.get("baths")),
            "acres": self._to_float(payload_data.get("acres")),
            "basement": payload_data.get("basement"),
            "latitude": self._to_float(lat),
            "longitude": self._to_float(lon),
            "search_min_list_price": min_price,
            "search_max_list_price": max_price,
        }

    def _parse_listing_card_fallback(
        self,
        node_html,
        text_lines,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
    ):
        payload_data = self._parse_open_map_payload(node_html)
        city, state, postal_code = self._extract_city_state_zip(text_lines, None)
        mls_id = payload_data.get("mls_id") or self._extract_mls_id_from_lines(text_lines) or payload_data.get("sysid")
        lat = payload_data.get("lat") or self._extract_pattern_group(LAT_INPUT_PATTERN, node_html)
        lon = payload_data.get("lon") or self._extract_pattern_group(LON_INPUT_PATTERN, node_html)
        address = payload_data.get("address") or self._extract_address_from_lines(text_lines)
        status = payload_data.get("status") or self._extract_status_text(node=None, text_lines=text_lines, node_html=node_html)

        return {
            "source": "gsmls",
            "mls_id": mls_id,
            "sys_id": payload_data.get("sysid"),
            "detail_url": self._build_detail_url(payload_data.get("sysid") or mls_id),
            "address": address,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "county": county_name,
            "county_code": county_code,
            "town": payload_data.get("town") or town_name,
            "town_code": payload_data.get("town_code") or town_code,
            "status": status,
            "list_price": self._to_int(payload_data.get("price")),
            "style": self._normalize_style(payload_data.get("style")),
            "rooms": self._to_int(payload_data.get("rooms")),
            "beds": self._to_int(payload_data.get("beds")),
            "baths": self._to_float(payload_data.get("baths")),
            "acres": self._to_float(payload_data.get("acres")),
            "basement": payload_data.get("basement"),
            "latitude": self._to_float(lat),
            "longitude": self._to_float(lon),
            "search_min_list_price": min_price,
            "search_max_list_price": max_price,
        }

    @staticmethod
    def _merge_listing_dicts(primary, fallback):
        merged = dict(fallback or {})
        for key, value in (primary or {}).items():
            if value not in (None, "", []):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        return merged

    @staticmethod
    def _build_detail_url(sysid):
        if not sysid:
            return None
        return f"https://www2.gsmls.com/publicsite/moredetails.do?method=moredetails&sysid={sysid}"

    @staticmethod
    def _listing_has_required_fields(listing):
        if not listing:
            return False
        return bool(listing.get("mls_id")) and bool(
            listing.get("address") or listing.get("city") or listing.get("town")
        )

    def _validate_listing(self, listing):
        errors = []
        if not listing.get("mls_id"):
            errors.append("missing_mls_id")
        if not (listing.get("address") or listing.get("city") or listing.get("town")):
            errors.append("missing_location")

        list_price = listing.get("list_price")
        if list_price is not None and list_price <= 0:
            listing["list_price"] = None
            errors.append("invalid_list_price")

        latitude = listing.get("latitude")
        longitude = listing.get("longitude")
        if latitude is not None and not (-90 <= latitude <= 90):
            listing["latitude"] = None
            errors.append("invalid_latitude")
        if longitude is not None and not (-180 <= longitude <= 180):
            listing["longitude"] = None
            errors.append("invalid_longitude")

        for field_name in ("rooms", "beds", "baths", "acres"):
            value = listing.get(field_name)
            if value is not None and value < 0:
                listing[field_name] = None
                errors.append(f"invalid_{field_name}")

        state = listing.get("state")
        if state:
            listing["state"] = state.strip().upper()
            if len(listing["state"]) != 2:
                errors.append("invalid_state")

        postal_code = listing.get("postal_code")
        if postal_code:
            listing["postal_code"] = postal_code.strip()
            if not re.match(r"^\d{5}(?:-\d{4})?$", listing["postal_code"]):
                errors.append("invalid_postal_code")

        return listing, errors

    def _build_failed_card_artifact(
        self,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
        reason,
        node,
    ):
        raw_html = node.get() or ""
        return {
            "__artifact_type__": "failed_card_drift",
            "source": self.name,
            "county": county_name,
            "county_code": county_code,
            "town": town_name,
            "town_code": town_code,
            "reason": reason,
            "search_min_list_price": min_price,
            "search_max_list_price": max_price,
            "selector_hints": self._selector_hints(node),
            "snippet": self._collapse_whitespace(raw_html)[:FAILED_CARD_SNIPPET_LIMIT],
            "html": raw_html[:FAILED_CARD_HTML_LIMIT],
            "text_lines": self._node_text_lines(node),
        }

    def _log_failed_card(self, artifact):
        selector_hints = json.dumps(artifact.get("selector_hints", {}), sort_keys=True)
        self.logger.warning(
            "GSMLS failed-card county=%s town=%s reason=%s selector_hints=%s snippet=%s",
            artifact.get("county"),
            artifact.get("town"),
            artifact.get("reason"),
            selector_hints,
            artifact.get("snippet"),
        )

    def _check_drift(
        self,
        county_name,
        town_name,
        min_price,
        max_price,
        display_count,
        page_seen,
        parsed_count,
    ):
        if display_count in (None, 0):
            return
        if self.drift_min_parse_ratio is None:
            return

        parse_ratio = page_seen / display_count if display_count else 0.0
        if parse_ratio >= self.drift_min_parse_ratio:
            return

        self.drift_events += 1
        message = (
            "GSMLS drift suspected county=%s town=%s min=%s max=%s "
            "display_count=%s page_seen=%s parsed_new=%s parse_ratio=%.3f threshold=%.3f"
        ) % (
            county_name,
            town_name,
            min_price,
            max_price,
            display_count,
            page_seen,
            parsed_count,
            parse_ratio,
            self.drift_min_parse_ratio,
        )
        if self.drift_fail_fast:
            raise CloseSpider(message)
        self.logger.warning(message)

    def _attempt_llm_repair(
        self,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
        node,
        reason,
    ):
        if not self.llm_repair_enabled:
            return None

        node_html = node.get() or ""
        text_lines = self._node_text_lines(node)
        prompt = self._build_llm_repair_prompt(
            county_name=county_name,
            county_code=county_code,
            town_name=town_name,
            town_code=town_code,
            min_price=min_price,
            max_price=max_price,
            reason=reason,
            node_html=node_html,
            text_lines=text_lines,
        )
        self.llm_repair_attempts += 1
        repaired_listing = self._ollama_repair_listing(prompt)
        if repaired_listing:
            repaired_listing.setdefault("county", county_name)
            repaired_listing.setdefault("county_code", county_code)
            repaired_listing.setdefault("town", town_name)
            repaired_listing.setdefault("town_code", town_code)
            repaired_listing.setdefault("search_min_list_price", min_price)
            repaired_listing.setdefault("search_max_list_price", max_price)
            self.llm_repair_successes += 1
            self.logger.info(
                "GSMLS LLM repair succeeded county=%s town=%s reason=%s mls_id=%s",
                county_name,
                town_name,
                reason,
                repaired_listing.get("mls_id"),
            )
        else:
            self.logger.warning(
                "GSMLS LLM repair failed county=%s town=%s reason=%s",
                county_name,
                town_name,
                reason,
            )
        return repaired_listing

    def _build_llm_repair_prompt(
        self,
        county_name,
        county_code,
        town_name,
        town_code,
        min_price,
        max_price,
        reason,
        node_html,
        text_lines,
    ):
        html_snippet = self._collapse_whitespace(node_html)[:LLM_REPAIR_PROMPT_SNIPPET_LIMIT]
        text_block = " | ".join(text_lines[:40])
        return (
            "Extract one GSMLS listing from this HTML fragment. "
            "Return only a single JSON object with these keys: "
            "mls_id, sys_id, address, city, state, postal_code, county, county_code, "
            "town, town_code, status, list_price, style, rooms, beds, baths, acres, "
            "basement, latitude, longitude. "
            "Use null for unknown values. Do not include markdown.\n"
            f"Context county={county_name} county_code={county_code} "
            f"town={town_name} town_code={town_code} "
            f"search_min_list_price={min_price} search_max_list_price={max_price} "
            f"failure_reason={reason}\n"
            f"Visible text: {text_block}\n"
            f"HTML: {html_snippet}"
        )

    def _ollama_repair_listing(self, prompt):
        url = f"{self.llm_repair_url}/api/generate"
        payload = {
            "model": self.llm_repair_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        request_body = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.llm_repair_timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except (error.URLError, TimeoutError, OSError) as exc:
            self.logger.warning("GSMLS Ollama request failed: %s", exc)
            return None

        try:
            outer = json.loads(response_body)
            raw_response = outer.get("response", "")
            if not raw_response:
                return None
            parsed = json.loads(raw_response)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.logger.warning("GSMLS Ollama response parse failed: %s", exc)
            return None

        if not isinstance(parsed, dict):
            return None

        listing = {
            "source": "gsmls",
            "mls_id": self._clean_string(parsed.get("mls_id")),
            "sys_id": self._clean_string(parsed.get("sys_id")),
            "detail_url": self._build_detail_url(
                self._clean_string(parsed.get("sys_id")) or self._clean_string(parsed.get("mls_id"))
            ),
            "address": self._clean_string(parsed.get("address")),
            "city": self._clean_string(parsed.get("city")),
            "state": self._clean_string(parsed.get("state")),
            "postal_code": self._clean_string(parsed.get("postal_code")),
            "county": self._clean_string(parsed.get("county")),
            "county_code": self._clean_string(parsed.get("county_code")),
            "town": self._clean_string(parsed.get("town")),
            "town_code": self._clean_string(parsed.get("town_code")),
            "status": self._clean_string(parsed.get("status")),
            "list_price": self._to_int(parsed.get("list_price")),
            "style": self._normalize_style(parsed.get("style")),
            "rooms": self._to_int(parsed.get("rooms")),
            "beds": self._to_int(parsed.get("beds")),
            "baths": self._to_float(parsed.get("baths")),
            "acres": self._to_float(parsed.get("acres")),
            "basement": self._clean_string(parsed.get("basement")),
            "latitude": self._to_float(parsed.get("latitude")),
            "longitude": self._to_float(parsed.get("longitude")),
        }
        if not listing.get("county"):
            listing["county"] = None
        return listing

    @staticmethod
    def _node_text_lines(node):
        return [t.strip() for t in node.xpath(".//text()").getall() if t and t.strip()]

    def _selector_hints(self, node):
        node_html = node.get() or ""
        hints = {
            "address_xpath": self._first_matching_selector(
                node,
                [
                    './/a[contains(@class, "address")]',
                    './/a[contains(@href, "openmapfromadd")]',
                    './/a[contains(@href, "moredetails")]',
                    './/a',
                ],
            ),
            "address_css": self._selector_xpath_to_css_hint(
                self._first_matching_selector(
                    node,
                    [
                        './/a[contains(@class, "address")]',
                        './/a[contains(@href, "openmapfromadd")]',
                        './/a[contains(@href, "moredetails")]',
                    ],
                )
            ),
            "mls_xpath": self._first_matching_selector(
                node,
                [
                    './/*[contains(normalize-space(.), "MLS#")]',
                    './/text()[contains(., "MLS#")]/parent::*',
                ],
            ),
            "status_xpath": self._first_matching_selector(
                node,
                [
                    './/*[contains(@class, "status")]',
                    './/*[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "ACTIVE")]',
                    './/*[contains(translate(normalize-space(.), "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "PENDING")]',
                ],
            ),
            "lat_xpath": self._first_matching_selector(
                node,
                [
                    './/input[@name="latitude"]',
                    './/input[contains(@id, "latitude")]',
                ],
            ),
            "lon_xpath": self._first_matching_selector(
                node,
                [
                    './/input[@name="longitude"]',
                    './/input[contains(@id, "longitude")]',
                ],
            ),
            "city_state_zip_xpath": self._first_matching_selector(
                node,
                [
                    './div',
                    './/*[contains(text(), ", NJ ")]',
                ],
            ),
            "openmap_href_present": bool(OPEN_MAP_PATTERN.search(node_html)),
            "latitude_input_present": bool(LAT_INPUT_PATTERN.search(node_html)),
            "longitude_input_present": bool(LON_INPUT_PATTERN.search(node_html)),
            "mls_text_present": "MLS#" in node_html,
        }
        return {key: value for key, value in hints.items() if value not in (None, "", False)}

    def _result_card_nodes(self, selector):
        selector_candidates = [
            '//div[contains(@class, "card-address")]',
            '//a[contains(@href, "openmapfromadd")]/ancestor::div[.//input[@name="latitude"] and .//input[@name="longitude"]][1]',
            '//a[contains(@href, "moredetails")]/ancestor::div[.//text()[contains(., "MLS#")]][1]',
        ]
        nodes = []
        seen_html = set()
        for xpath_expr in selector_candidates:
            for node in selector.xpath(xpath_expr):
                node_html = node.get() or ""
                if not node_html or node_html in seen_html:
                    continue
                seen_html.add(node_html)
                nodes.append(node)
        return nodes

    def _extract_labeled_lines(self, node, text_lines):
        plain_div_text = [t.strip() for t in node.xpath("./div/text()").getall() if t.strip()]
        city_line = plain_div_text[0] if plain_div_text else ""
        mls_line = plain_div_text[1] if len(plain_div_text) > 1 else ""
        if not city_line:
            city_line = next((line for line in text_lines if CITY_STATE_ZIP_PATTERN.match(line)), "")
        if not mls_line:
            mls_line = next((line for line in text_lines if "MLS#" in line), "")
        return city_line, mls_line

    @staticmethod
    def _collapse_whitespace(value):
        return " ".join((value or "").split())

    def _first_joined_text(self, node, selectors):
        for selector in selectors:
            value = self._collapse_whitespace(" ".join(node.xpath(selector).getall()))
            if value:
                return value
        return None

    @staticmethod
    def _first_attr(node, selectors):
        for selector in selectors:
            value = node.xpath(selector).get()
            if value:
                return value.strip()
        return None

    @staticmethod
    def _first_matching_selector(node, selectors):
        for selector in selectors:
            if node.xpath(selector):
                return selector
        return None

    @staticmethod
    def _selector_xpath_to_css_hint(xpath_selector):
        if xpath_selector == './/a[contains(@class, "address")]':
            return "a.address"
        if xpath_selector == './/a[contains(@href, "openmapfromadd")]':
            return 'a[href*="openmapfromadd"]'
        if xpath_selector == './/a[contains(@href, "moredetails")]':
            return 'a[href*="moredetails"]'
        return None

    def _extract_city_state_zip(self, text_lines, city_line):
        candidates = []
        if city_line:
            candidates.append(city_line)
        candidates.extend(text_lines)
        for line in candidates:
            city_match = CITY_STATE_ZIP_PATTERN.match(line or "")
            if city_match:
                return (
                    city_match.group(1).strip(),
                    city_match.group(2).strip(),
                    city_match.group(3).strip(),
                )
        return None, None, None

    def _extract_status_text(self, node, text_lines, node_html):
        candidates = []
        if node is not None:
            candidates.extend(
                [
                    self._first_joined_text(
                        node,
                        [
                            './/div[contains(@class, "status")]//text()',
                            './/*[contains(@class, "status")]//text()',
                        ],
                    )
                ]
            )
        candidates.extend(text_lines)
        candidates.append(self._collapse_whitespace(node_html))
        for candidate in candidates:
            if not candidate:
                continue
            match = STATUS_TEXT_PATTERN.search(candidate)
            if match:
                return match.group(1).upper()
        return None

    @staticmethod
    def _clean_string(value):
        if value in (None, ""):
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _normalize_style(self, value):
        cleaned = self._clean_string(value)
        if not cleaned:
            return None
        if cleaned.lower() in NON_STYLE_VALUES:
            return None
        return cleaned

    def _extract_detail_field_map(self, selector):
        field_map = {}
        for node in selector.xpath('//span[contains(@class, "field-label")]/parent::div'):
            label = self._collapse_whitespace(
                " ".join(node.xpath('./span[contains(@class, "field-label")][1]//text()').getall())
            )
            label = (label or "").rstrip(":").strip()
            if not label:
                continue
            direct_text = self._collapse_whitespace(" ".join(node.xpath("./text()").getall()))
            nested_text = self._collapse_whitespace(
                " ".join(
                    node.xpath('./*[not(self::span[contains(@class, "field-label")])]//text()').getall()
                )
            )
            value = self._collapse_whitespace(" ".join(v for v in (direct_text, nested_text) if v))
            if not value:
                continue
            if label in field_map and field_map[label] != value:
                field_map[label] = f"{field_map[label]} | {value}"
            else:
                field_map[label] = value
        return field_map

    def _extract_overview_text(self, selector):
        overview_nodes = selector.xpath('//h3[normalize-space()="Overview"]/following-sibling::text()').getall()
        overview = self._collapse_whitespace(" ".join(overview_nodes))
        if overview:
            return overview
        return None

    def _extract_broker_contact(self, selector):
        office_name = None
        office_phone = None
        agent_name = None

        broker_lines = [
            self._collapse_whitespace(text)
            for text in selector.xpath('//div[contains(@class, "broker-info")]//text()').getall()
        ]
        broker_lines = [line for line in broker_lines if line]
        for line in broker_lines:
            if not office_phone:
                phone_match = PHONE_PATTERN.search(line)
                if phone_match:
                    office_phone = self._collapse_whitespace(phone_match.group(0))
            if not office_name and not PHONE_PATTERN.search(line):
                office_name = line

        agent_lines = [
            self._collapse_whitespace(text)
            for text in selector.xpath('//div[contains(@class, "agent-info")]//text()').getall()
        ]
        agent_lines = [line for line in agent_lines if line and line.lower() != "listing agent"]
        if agent_lines:
            agent_name = agent_lines[0]

        return office_name, office_phone, agent_name

    @staticmethod
    def _extract_pattern_group(pattern, text):
        match = pattern.search(text or "")
        if not match:
            return None
        return match.group(1).strip()

    def _extract_mls_id_from_lines(self, text_lines):
        for line in text_lines:
            match = MLS_PATTERN.search(line or "")
            if match:
                return match.group(1)
        return None

    def _extract_address_from_lines(self, text_lines):
        for line in text_lines:
            line = self._collapse_whitespace(line)
            if not line:
                continue
            if "MLS#" in line:
                continue
            if CITY_STATE_ZIP_PATTERN.match(line):
                continue
            if re.search(r"\d", line):
                return line
        return None

    def _parse_open_map_payload(self, source_text):
        match = OPEN_MAP_PATTERN.search(source_text or "")
        if not match:
            return {}

        payload = match.group(3)
        sysid = match.group(4).strip() if match.group(4) else None
        parts = payload.split(":") if payload else []
        return {
            "lat": match.group(1).strip() if match.group(1) else None,
            "lon": match.group(2).strip() if match.group(2) else None,
            "mls_id": parts[0].strip() if len(parts) > 0 and parts[0].strip() else None,
            "status": parts[2].strip() if len(parts) > 2 and parts[2].strip() else None,
            "town": parts[3].strip() if len(parts) > 3 and parts[3].strip() else None,
            "town_code": parts[4].strip() if len(parts) > 4 and parts[4].strip() else None,
            "address": parts[5].strip() if len(parts) > 5 and parts[5].strip() else None,
            "price": parts[6].strip() if len(parts) > 6 and parts[6].strip() else None,
            "style": parts[7].strip() if len(parts) > 7 and parts[7].strip() else None,
            "rooms": parts[8].strip() if len(parts) > 8 and parts[8].strip() else None,
            "beds": parts[9].strip() if len(parts) > 9 and parts[9].strip() else None,
            "baths": parts[10].strip() if len(parts) > 10 and parts[10].strip() else None,
            "acres": parts[11].strip() if len(parts) > 11 and parts[11].strip() else None,
            "basement": parts[12].strip() if len(parts) > 12 and parts[12].strip() else None,
            "sysid": sysid,
        }

    @staticmethod
    def _extract_over_limit_count(text):
        match = OVER_LIMIT_PATTERN.search(text or "")
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_display_count(text):
        match = DISPLAY_COUNT_PATTERN.search(text or "")
        if not match:
            return None
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

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
    def _to_int(value):
        if value in (None, ""):
            return None
        try:
            normalized = re.sub(r"[^0-9.\-]", "", str(value))
            if normalized in ("", "-", ".", "-."):
                return None
            return int(float(normalized))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value):
        if value in (None, ""):
            return None
        try:
            normalized = re.sub(r"[^0-9.\-]", "", str(value))
            if normalized in ("", "-", ".", "-."):
                return None
            return float(normalized)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_bound(value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _choose_split_value(self, min_price, max_price):
        min_bound = self._normalize_bound(min_price)
        max_bound = self._normalize_bound(max_price)

        candidates = []
        for point in PRICE_SPLIT_POINTS:
            if min_bound is not None and point <= min_bound:
                continue
            if max_bound is not None and point >= max_bound:
                continue
            candidates.append(point)

        if not candidates:
            return None

        return candidates[len(candidates) // 2]
