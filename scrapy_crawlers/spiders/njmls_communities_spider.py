import re
from html import unescape
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

import scrapy
from scrapy.http import TextResponse

from items import CommunityInfoItem
from spiders.env_config import build_proxy_url, get_env

COMM_SELECT_URL = "https://www.njmls.com/communities/views/xhr.commselect.cfm"
TOWN_INFO_URL = "https://www.njmls.com/communities/index.cfm"
SCHOOL_DETAILS_URL = "https://www.njmls.com/communities/xhr/xhrSchoolDetails.cfm"
AMENITIES_URL = "https://www.njmls.com/communities/xhr/xhrAmenities.cfm"

# Ordered list of all 21 NJ counties (uppercase, matching the county= query param)
NJ_COUNTIES = [
    "ATLANTIC", "BERGEN", "BURLINGTON", "CAMDEN", "CAPE MAY",
    "CUMBERLAND", "ESSEX", "GLOUCESTER", "HUDSON", "HUNTERDON",
    "MERCER", "MIDDLESEX", "MONMOUTH", "MORRIS", "OCEAN",
    "PASSAIC", "SALEM", "SOMERSET", "SUSSEX", "UNION", "WARREN",
]

_STRONG_LABEL_RE = re.compile(r"<strong>[^<]*</strong>", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_SCHOOL_ID_RE = re.compile(r"\bschool:\s*(\d+)")
_SCHOOL_ROW_ID_RE = re.compile(r"SchoolRow(\d+)")
_AMENITY_META_RE = re.compile(
    r"amenitycol:\s*'(?P<col>[^']+)'.*?"
    r"amenityval:\s*'(?P<val>[^']+)'.*?"
    r"category:\s*'(?P<category>[^']+)'.*?"
    r"activeLinkId:(?P<link_id>\d+)",
    re.DOTALL,
)
_AMENITY_DATA_RE = re.compile(
    r"AmenitiesRow \{[^}]*lat:\s*'(?P<lat>[^']+)'[^}]*lng:\s*'(?P<lng>[^']+)'"
    r"[^}]*name:\s*'(?P<name>[^']+)'[^}]*address:\s*'(?P<address>[^']+)'"
    r"(?:[^}]*phone:\s*'(?P<phone>[^']*)')?",
    re.DOTALL,
)
_LAT_LNG_RE = re.compile(
    r"QuickFactsMap[^{]*\{[^}]*lat:\s*['\"](?P<lat>-?[\d.]+)['\"][^}]*lng:\s*['\"](?P<lng>-?[\d.]+)['\"]",
    re.DOTALL,
)
_TOTAL_PAGES_RE = re.compile(r"of\s+(\d+)", re.IGNORECASE)
_GRADE_INPUT_RE = re.compile(r'id="(?P<grade>[^"]+)"\s+value="(?P<count>\d+)"')
_COMMSELECT_TOWN_RE = re.compile(
    r"^\s*(?P<town>.+?),\s*NJ\s*\((?P<county>.+?)\s+COUNTY\)\s*$",
    re.IGNORECASE,
)
_SINGLE_QUOTED_VALUE_RE = re.compile(r"'((?:\\'|[^'])*)'")
_JS_SINGLE_QUOTED_RE = r"((?:\\'|[^'])*)"


def _clean(text):
    if not text:
        return None
    cleaned = _STRONG_LABEL_RE.sub("", text)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _clean_js_string(text):
    if text is None:
        return None
    return unescape(str(text).replace("\\'", "'").replace('\\"', '"')).strip() or None


def _to_float(text):
    if not text:
        return None
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(text)))
    except (TypeError, ValueError):
        return None


def _to_int(text):
    v = _to_float(text)
    return int(v) if v is not None else None


def _parse_label_value_cell(cell):
    raw = cell.get() or ""
    label_texts = [
        t.strip()
        for t in cell.css("strong:first-child::text, strong:first-child ::text").getall()
        if t.strip()
    ]
    if label_texts:
        label_raw = " ".join(label_texts)
    else:
        label_match = re.search(r"<strong[^>]*>(.*?)</strong>", raw, re.IGNORECASE | re.DOTALL)
        if not label_match:
            return None, None
        label_raw = re.sub(r"<[^>]+>", " ", label_match.group(1))

    label = _clean(label_raw.rstrip(":").strip())
    if not label:
        return None, None

    value_raw = _clean(" ".join(t.strip() for t in cell.css("::text").getall() if t.strip()))
    if not value_raw:
        return label, None

    value = re.sub(rf"^\s*{re.escape(label_raw.strip())}\s*", "", value_raw).strip()
    value = re.sub(rf"^\s*{re.escape(label)}\s*:?\s*", "", value).strip()
    return label, _clean(value)


def _parse_kv_table(selector, table_css):
    """Extract key/value pairs from a two-column table (strong label : plain value)."""
    result = {}
    for row in selector.css(f"{table_css} tr"):
        cells = row.css("td")
        for cell in cells:
            label, value = _parse_label_value_cell(cell)
            if label and value:
                result[label] = value
    return result


class NJMLSCommunitiesSpider(scrapy.Spider):
    name = "njmls_communities"
    allowed_domains = ["www.njmls.com"]

    custom_settings = {
        "ITEM_PIPELINES": {
            "pipelines.pipelines.DriftArtifactJsonLinesPipeline": 200,
            "pipelines.pipelines.CommunityInfoMongoPipeline": 300,
        },
        "DOWNLOAD_DELAY": 3,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "COOKIES_ENABLED": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 3,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.5,
    }

    def __init__(self, counties=None, towns=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_proxy = str(kwargs.get("disable_proxy", "")).strip().lower() in {"1", "true", "yes"}
        self.proxy_url = None if disable_proxy else build_proxy_url()
        # Optional spider args: -a counties=BERGEN,ESSEX or -a towns="SADDLE BROOK:BERGEN"
        self._filter_counties = (
            {c.strip().upper() for c in counties.split(",")} if counties else None
        )
        self._filter_towns = {}
        if towns:
            for entry in towns.split(";"):
                parts = entry.strip().split(":")
                if len(parts) == 2:
                    self._filter_towns[parts[0].strip().upper()] = parts[1].strip().upper()

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            COMM_SELECT_URL,
            callback=self.parse_town_list,
            errback=self._err_town_list,
            meta=self._proxy_meta(),
            dont_filter=True,
        )

    def _proxy_meta(self):
        return {"proxy": self.proxy_url} if self.proxy_url else {}

    def _err_town_list(self, failure):
        self.logger.error("Failed to fetch town list: %s", failure)

    # ------------------------------------------------------------------
    # Town list
    # ------------------------------------------------------------------

    def parse_town_list(self, response):
        """Parse the commselect HTML fragment and seed one request per (town, county)."""
        html = self._response_text(response)
        selector = scrapy.Selector(text=html)
        self.logger.debug("commselect raw body size=%d", len(html))

        towns = self._extract_towns(selector, html)
        if not towns:
            self.logger.error(
                "No towns parsed from commselect (status=%s, size=%d). "
                "Check the URL or parser.",
                response.status,
                len(html),
            )
            return

        self.logger.info("Found %d town+county combinations", len(towns))
        for town_name, county in towns:
            if self._filter_counties and county not in self._filter_counties:
                continue
            if self._filter_towns:
                expected_county = self._filter_towns.get(town_name.upper())
                if expected_county is None or expected_county != county:
                    continue
            meta = {"town_name": town_name, "county": county}
            meta.update(self._proxy_meta())
            yield scrapy.Request(
                url=(
                    f"{TOWN_INFO_URL}?action=dsp.towninfo"
                    f"&townname={quote_plus(town_name)}"
                    f"&view=facts"
                    f"&county={quote_plus(county)}"
                ),
                callback=self.parse_town_info,
                errback=self._make_errback("town_info", town_name, county),
                meta=meta,
            )

    def _extract_towns(self, selector, html=""):
        """
        Try multiple HTML structures that commselect.cfm may return:
          1. <optgroup label="BERGEN COUNTY"> with <option value="SADDLE BROOK">
          2. <optgroup label="BERGEN"> with <option value="SADDLE BROOK|BERGEN">
          3. h3 + checkbox inputs (same format as listings town modal)
          4. Plain <option> with data-county attribute
        """
        towns = []

        # Strategy 1: <optgroup label="COUNTY COUNTY"> / <optgroup label="COUNTY">
        for optgroup in selector.css("optgroup"):
            label = (optgroup.attrib.get("label") or "").strip().upper()
            county = re.sub(r"\s+COUNTY$", "", label).strip()
            if not county:
                continue
            for opt in optgroup.css("option"):
                raw_val = (opt.attrib.get("value") or "").strip()
                if not raw_val:
                    continue
                # May be "SADDLE BROOK|BERGEN" or just "SADDLE BROOK"
                parts = raw_val.split("|")
                town_name = parts[0].strip().upper()
                if len(parts) >= 2:
                    county = parts[1].strip().upper()
                if town_name:
                    towns.append((town_name, county))
            if towns:
                return towns

        # Strategy 2: plain <option> with data-county attribute
        for opt in selector.css("option[data-county]"):
            town_name = (opt.attrib.get("value") or opt.css("::text").get() or "").strip().upper()
            county = (opt.attrib.get("data-county") or "").strip().upper()
            if town_name and county:
                towns.append((town_name, county))
        if towns:
            return towns

        # Strategy 3: checkbox inputs grouped under h3/h4 county headers
        #   (same format as listings/index.cfm?action=xhr.multiple_town_select_new)
        for header in selector.css("h3, h4"):
            header_text = (header.css("::text").get() or "").strip().upper()
            county = re.sub(r"\s+COUNTY.*$", "", header_text).strip()
            if county not in NJ_COUNTIES:
                continue
            container = header.xpath("following-sibling::div[1] | following-sibling::ul[1]")
            for inp in container.css("input[type='checkbox']"):
                raw_val = (inp.attrib.get("value") or "").strip()
                # Value may be "SADDLE BROOK, NJ, BERGEN, 07663"
                parts = [p.strip().upper() for p in raw_val.split(",")]
                if parts:
                    town_name = parts[0]
                    if len(parts) >= 3:
                        county = parts[2]
                    if town_name:
                        towns.append((town_name, county))
        if towns:
            return towns

        # Strategy 4: any option element whose text matches a town pattern;
        # try to infer county from nearest optgroup or preceding header
        for opt in selector.css("option"):
            val = (opt.attrib.get("value") or "").strip()
            # skip blanks and county-level options
            if not val or re.search(r"county", val, re.IGNORECASE):
                continue
            parts = re.split(r"[|,]", val)
            town_name = parts[0].strip().upper()
            county = parts[2].strip().upper() if len(parts) >= 3 else parts[1].strip().upper() if len(parts) >= 2 else ""
            # Discard if county not in known list and not a county-like string
            if county and county not in NJ_COUNTIES:
                county = ""
            if town_name:
                towns.append((town_name, county or ""))

        if towns:
            return towns

        # Strategy 5: current commselect.cfm response is a JSON-like ColdFusion
        # payload with data.PROPSEARCHNAME entries:
        #   'SADDLE BROOK, NJ (BERGEN COUNTY)'
        data_match = re.search(
            r'"PROPSEARCHNAME"\s*:\s*\[(?P<values>.*?)\]\s*,\s*"ZIP"',
            html or "",
            re.DOTALL | re.IGNORECASE,
        )
        values_block = data_match.group("values") if data_match else html or ""
        seen = set()
        for raw_value in _SINGLE_QUOTED_VALUE_RE.findall(values_block):
            value = _clean_js_string(raw_value)
            if not value:
                continue
            match = _COMMSELECT_TOWN_RE.match(value)
            if not match:
                continue
            town_name = _clean(match.group("town"))
            county = _clean(match.group("county"))
            if not town_name or not county:
                continue
            key = (town_name.upper(), county.upper())
            if key in seen:
                continue
            seen.add(key)
            towns.append(key)

        return towns

    # ------------------------------------------------------------------
    # Town info page
    # ------------------------------------------------------------------

    def parse_town_info(self, response):
        response = self._as_text_response(response)
        town_name = response.meta["town_name"]
        county = response.meta["county"]

        demographics = self._parse_demographics(response)
        geography = self._parse_geography(response)
        school_entries, school_ids = self._parse_school_list(response)
        report_links = self._parse_report_links(response)
        amenity_metas, zip_code = self._parse_amenity_categories(response)
        lat, lng = self._parse_lat_lng(response)

        item = CommunityInfoItem(
            community_key=f"{county.lower()}_{town_name.lower()}".replace(" ", "_"),
            source="njmls",
            town_name=town_name.title(),
            county=county.title(),
            state="NJ",
            zip_code=zip_code,
            latitude=lat,
            longitude=lng,
            demographics=demographics,
            geography=geography,
            schools=school_entries,
            school_report_links=report_links,
            amenities={},
            crawled_at=datetime.now(timezone.utc).isoformat(),
        )

        total_async = len(school_ids) + len(amenity_metas)
        if total_async == 0:
            yield item
            return

        # Shared mutable state — all async callbacks share the same item + counter.
        pending = {"count": total_async}

        for school_id in school_ids:
            meta = {
                "item": item,
                "pending": pending,
                "school_id": school_id,
            }
            meta.update(self._proxy_meta())
            yield scrapy.Request(
                url=f"{SCHOOL_DETAILS_URL}?school={school_id}",
                callback=self.parse_school_details,
                errback=self._make_decrement_errback(pending, item),
                headers={
                    "accept": "text/html, */*; q=0.01",
                    "x-requested-with": "XMLHttpRequest",
                    "referer": response.url,
                },
                meta=meta,
            )

        for amen in amenity_metas:
            meta = {
                "item": item,
                "pending": pending,
                "amen_meta": amen,
                "page": 1,
            }
            meta.update(self._proxy_meta())
            yield scrapy.Request(
                url=self._amenity_url(amen, page=1),
                callback=self.parse_amenities,
                errback=self._make_decrement_errback(pending, item),
                headers={
                    "accept": "text/html, */*; q=0.01",
                    "x-requested-with": "XMLHttpRequest",
                    "referer": response.url,
                },
                meta=meta,
            )

    # ------------------------------------------------------------------
    # School details
    # ------------------------------------------------------------------

    def parse_school_details(self, response):
        response = self._as_text_response(response)
        item = response.meta["item"]
        school_id = str(response.meta["school_id"])
        pending = response.meta["pending"]

        details = self._parse_school_details_html(response)
        details["school_id"] = school_id

        # Merge into the matching stub in item["schools"]
        for i, s in enumerate(item["schools"]):
            if str(s.get("school_id")) == school_id:
                item["schools"][i] = {**s, **details}
                break

        pending["count"] -= 1
        if pending["count"] == 0:
            yield item

    # ------------------------------------------------------------------
    # Amenities
    # ------------------------------------------------------------------

    def parse_amenities(self, response):
        response = self._as_text_response(response)
        item = response.meta["item"]
        pending = response.meta["pending"]
        amen_meta = response.meta["amen_meta"]
        page = response.meta["page"]
        category = amen_meta["category"]

        entries = self._parse_amenity_items(response)
        if category not in item["amenities"]:
            item["amenities"][category] = []
        item["amenities"][category].extend(entries)

        # Check for more pages
        total_pages = self._total_pages(response)
        if page < total_pages:
            next_page = page + 1
            pending["count"] += 1  # pre-increment before yielding, then decrement below
            meta = {
                "item": item,
                "pending": pending,
                "amen_meta": amen_meta,
                "page": next_page,
            }
            meta.update(self._proxy_meta())
            yield scrapy.Request(
                url=self._amenity_url(amen_meta, page=next_page),
                callback=self.parse_amenities,
                errback=self._make_decrement_errback(pending, item),
                headers={
                    "accept": "text/html, */*; q=0.01",
                    "x-requested-with": "XMLHttpRequest",
                    "referer": response.url,
                },
                meta=meta,
            )

        pending["count"] -= 1
        if pending["count"] == 0:
            yield item

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _response_text(response):
        try:
            return response.text
        except Exception:
            return response.body.decode("utf-8", errors="replace")

    @classmethod
    def _as_text_response(cls, response):
        try:
            response.css("body")
            return response
        except Exception:
            html = cls._response_text(response)
            return TextResponse(
                url=response.url,
                body=html.encode("utf-8"),
                encoding="utf-8",
                request=response.request,
            )

    @staticmethod
    def _parse_demographics(response):
        demo = {}

        def _row_kv(tbl_id):
            rows = {}
            for row in response.css(f"#{tbl_id} tr"):
                for cell in row.css("td"):
                    label, value = _parse_label_value_cell(cell)
                    if label and value:
                        rows[label] = value
            return rows

        for tbl_id in (
            "DemograhpicsQuickFactsTbl",
            "DemograhpicsPopulationTbl",
            "DemograhpicsHouseholdsTbl",
            "DemograhpicsIncomeTbl",
            "DemograhpicsEducationTbl",
        ):
            demo.update(_row_kv(tbl_id))

        # Attempt numeric coercion on known fields
        numeric_keys = {
            "Population", "Population Density / Mile", "Median Age",
            "Number of Households", "Average Household Size",
            "Households with Children", "Households without Children",
            "Family Households", "Non-Family Households",
            "Male Population", "Female Population",
        }
        money_keys = {
            "Median Household Income", "Average Household Income",
            "Per Capita Income", "Average Total Household Expenditure",
        }
        pct_keys = {"% Change in Population Since 2010"}
        for k in list(demo.keys()):
            v = demo[k]
            if k in numeric_keys:
                demo[k] = _to_float(v.replace(",", ""))
            elif k in money_keys:
                demo[k] = _to_int(re.sub(r"[^0-9.]", "", v))
            elif k in pct_keys:
                demo[k] = _to_float(v.replace("%", ""))
            elif k == "Educational Climate Index":
                demo[k] = _to_int(v)
        return demo

    @staticmethod
    def _parse_geography(response):
        geo = {}
        for row in response.css("#GeographyTbl tr"):
            for cell in row.css("td"):
                label, value = _parse_label_value_cell(cell)
                if label and value:
                    geo[label] = _to_float(value)
        return geo

    @staticmethod
    def _parse_school_list(response):
        """Return (school_entries, school_ids) from the main town page schools table."""
        entries = []
        ids = []
        seen = set()

        for row in response.css("table.obSchools tbody tr.obSchoolTableRow"):
            row_id = row.attrib.get("id", "")
            row_id_match = _SCHOOL_ROW_ID_RE.search(row_id)
            link_class = " ".join(
                row.css("a.obSchoolDetailLink::attr(class), a.obSchoolDetailLinkMobile::attr(class)").getall()
            )
            class_match = _SCHOOL_ID_RE.search(link_class)
            school_id = class_match.group(1) if class_match else row_id_match.group(1) if row_id_match else None
            if not school_id or school_id in seen:
                continue
            name = _clean(
                row.css("td:first-child::text, td.schools-label::text").get() or ""
            )
            if not name:
                text_parts = [t.strip() for t in row.css("td::text").getall() if t.strip()]
                name = _clean(text_parts[0]) if text_parts else None
            if name:
                entries.append({"school_id": school_id, "name": name})
                ids.append(school_id)
                seen.add(school_id)

        # Fallback: parse from row IDs even if classes/links change.
        if not ids:
            for row in response.css("tr[id^='SchoolRow']"):
                m = _SCHOOL_ROW_ID_RE.search(row.attrib.get("id", ""))
                if m:
                    school_id = m.group(1)
                    if school_id in seen:
                        continue
                    name = _clean(row.css("td:first-child::text, td.schools-label::text").get() or "")
                    entries.append({"school_id": school_id, "name": name or ""})
                    ids.append(school_id)
                    seen.add(school_id)
        return entries, ids

    @staticmethod
    def _parse_report_links(response):
        """Return list of {name, url} from the NJ performance reports table."""
        links = []
        seen = set()
        for row in response.css("table.schoolReportCard tbody tr"):
            cells = row.css("td")
            if len(cells) < 2:
                continue
            name = _clean(cells[0].css("::text").get() or "")
            url = cells[1].css("a[href]").attrib.get("href", "").strip()
            if name and url:
                absolute_url = urljoin(response.url, url)
                key = (name, absolute_url)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"name": name, "url": absolute_url})
        return links

    @staticmethod
    def _parse_amenity_categories(response):
        """Return (list_of_amen_meta_dicts, zip_code)."""
        metas = []
        zip_code = None
        seen = set()
        for link in response.css("a.obAmenitiesLink, a.obAmenitiesLinkMobile"):
            class_attr = link.attrib.get("class", "")
            m = _AMENITY_META_RE.search(class_attr)
            if m:
                amen = {
                    "col": m.group("col"),
                    "val": m.group("val"),
                    "category": m.group("category"),
                    "link_id": m.group("link_id"),
                }
                key = (amen["col"], amen["val"], amen["category"], amen["link_id"])
                if key in seen:
                    continue
                seen.add(key)
                if zip_code is None:
                    zip_code = amen["val"]
                metas.append(amen)
        return metas, zip_code

    @staticmethod
    def _parse_lat_lng(response):
        m = _LAT_LNG_RE.search(response.text)
        if m:
            return _to_float(m.group("lat")), _to_float(m.group("lng"))
        return None, None

    @staticmethod
    def _parse_school_details_html(response):
        """Parse the xhrSchoolDetails.cfm HTML fragment into a dict."""
        data = {}

        # School name from h2
        h2 = response.css("h2::text").get() or ""
        name_match = re.search(r"School Details\s*-\s*(.+)", h2, re.IGNORECASE)
        if name_match:
            data["name"] = name_match.group(1).strip()

        # All tables use obSchoolTable class
        for tbl in response.css("table.obSchoolTable"):
            header = _clean(" ".join(t.strip() for t in tbl.css("th ::text, th::text").getall() if t.strip()))
            if not header:
                continue
            header_upper = header.upper()

            if "GENERAL INFORMATION" in header_upper:
                for row in tbl.css("tr"):
                    cells = row.css("td")
                    if len(cells) < 2:
                        continue
                    key, _ = _parse_label_value_cell(cells[0])
                    if not key:
                        continue
                    key = key.rstrip(":")
                    val_cell = cells[1]
                    # Phone, website, address may have sub-tags
                    if key == "Website":
                        data["website"] = (val_cell.css("a::attr(href)").get() or "").strip() or _clean(val_cell.css("::text").get() or "")
                    elif key == "Address":
                        addr_parts = [t.strip() for t in val_cell.css("::text").getall() if t.strip()]
                        data["address"] = " ".join(addr_parts)
                    else:
                        data[_to_snake(key)] = _clean(val_cell.css("::text").get() or "")

            elif "SCHOOL PROFILE" in header_upper:
                for row in tbl.css("tr"):
                    cells = row.css("td")
                    if len(cells) < 2:
                        continue
                    key, _ = _parse_label_value_cell(cells[0])
                    key = key.rstrip(":") if key else None
                    val = _clean(" ".join(t.strip() for t in cells[1].css("::text").getall() if t.strip()))
                    if key and val:
                        data[_to_snake(key)] = val

            elif "ENROLLMENT AND STAFFING" in header_upper:
                for row in tbl.css("tr"):
                    cells = row.css("td")
                    if len(cells) < 2:
                        continue
                    key, _ = _parse_label_value_cell(cells[0])
                    key = key.rstrip(":") if key else None
                    val = _clean(" ".join(t.strip() for t in cells[1].css("::text").getall() if t.strip()))
                    if key and val:
                        numeric_enrollment = {"Number of Students", "Full Time Teachers", "Student/Teacher Ratio"}
                        data[_to_snake(key)] = _to_int(val) if key in numeric_enrollment else val

            elif "STUDENTS PER GRADE" in header_upper:
                per_grade = {}
                for inp in tbl.css("input[type='hidden']"):
                    grade_id = inp.attrib.get("id", "")
                    count_val = inp.attrib.get("value", "")
                    if grade_id and count_val:
                        count = _to_int(count_val)
                        if count is not None and count > 0:
                            per_grade[grade_id] = count
                if per_grade:
                    data["students_per_grade"] = per_grade

            elif "FEATURES AND PROGRAMS" in header_upper:
                programs = [_clean(li.css("::text").get() or "") for li in tbl.css("li")]
                data["programs"] = [p for p in programs if p]

        # Lat/lng from SchoolMap metadata
        school_map = response.css("#SchoolMap")
        if school_map:
            class_attr = school_map.attrib.get("class", "")
            lat_m = re.search(r"lat:\s*([-\d.]+)", class_attr)
            lng_m = re.search(r"lng:\s*([-\d.]+)", class_attr)
            if lat_m:
                data["latitude"] = _to_float(lat_m.group(1))
            if lng_m:
                data["longitude"] = _to_float(lng_m.group(1))

        return data

    @staticmethod
    def _parse_amenity_items(response):
        """Parse amenity listing rows from an xhrAmenities.cfm response."""
        entries = []
        for div in response.css("div.AmenitiesRow, div[class*='AmenitiesRow']"):
            class_attr = div.attrib.get("class", "")
            lat = lng = name = address = phone = None

            meta_match = re.search(
                r"\{[^}]*lat:\s*'(?P<lat>[^']*)'[^}]*lng:\s*'(?P<lng>[^']*)'[^}]*"
                rf"name:\s*'(?P<name>{_JS_SINGLE_QUOTED_RE})'[^}}]*"
                rf"address:\s*'(?P<address>{_JS_SINGLE_QUOTED_RE})'"
                rf"(?:[^}}]*phone:\s*'(?P<phone>{_JS_SINGLE_QUOTED_RE})')?",
                class_attr,
                re.DOTALL,
            )
            if meta_match:
                lat = _to_float(meta_match.group("lat"))
                lng = _to_float(meta_match.group("lng"))
                name = _clean_js_string(meta_match.group("name"))
                address = _clean_js_string(meta_match.group("address"))
                phone = _clean_js_string(meta_match.group("phone"))

            # Phone from visible text
            phone_match = re.search(r"Phone:\s*([\d\-().+\s]+)", div.css("::text").getall().__repr__() or "")
            if not phone_match:
                top_text = " ".join(div.css("div.top ::text").getall())
                phone_match = re.search(r"Phone:\s*([\d\-().+\s]{7,20})", top_text)
            if phone_match:
                phone = phone_match.group(1).strip()

            if not name:
                name = _clean(div.css("div.top span.l::text, div.top a::text").get() or "")
            if not address:
                address = _clean(div.css("div.bottom::text").get() or "")

            if name:
                entries.append({
                    "name": name,
                    "address": address,
                    "phone": phone,
                    "latitude": lat,
                    "longitude": lng,
                })
        return entries

    @staticmethod
    def _total_pages(response):
        m = re.search(r"of\s+(\d+)", response.css("#pager").get() or "", re.IGNORECASE)
        return int(m.group(1)) if m else 1

    @staticmethod
    def _amenity_url(amen, page):
        return (
            f"{AMENITIES_URL}"
            f"?amenitycol={quote_plus(amen['col'])}"
            f"&amenityval={quote_plus(amen['val'])}"
            f"&category={quote_plus(amen['category'])}"
            f"&strt=1&show=20&currentPage={page}"
            f"&activeLinkId={amen['link_id']}"
        )

    # ------------------------------------------------------------------
    # Errback factories
    # ------------------------------------------------------------------

    def _make_errback(self, stage, town_name, county):
        def errback(failure):
            self.logger.warning(
                "Failed %s for %s/%s: %s", stage, town_name, county, failure
            )
        return errback

    @staticmethod
    def _make_decrement_errback(pending, item):
        """On failure, decrement the pending counter and yield item if all done."""
        def errback(failure):
            pending["count"] -= 1
            if pending["count"] == 0:
                return [item]
            return []
        return errback


def _to_snake(text):
    """Convert a human-readable label to a snake_case key."""
    if not text:
        return text
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s
