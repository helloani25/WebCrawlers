import scrapy
import json
import uuid
import math
import copy
from datetime import datetime, timezone
from urllib.parse import urljoin
from scrapy.http import Request
from spiders.env_config import build_proxy_url, get_env


class BhgreSpider(scrapy.Spider):
    """Spider for BHGRE property listings in New Jersey"""

    name = 'bhgre'
    allowed_domains = ['www.bhgre.com']
    handle_httpstatus_list = [401]

    custom_settings = {
        "COOKIES_ENABLED": True,
        "CURL_IMPERSONATE": "chrome110",
        "DEFAULT_REQUEST_HEADERS": {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://www.bhgre.com",
            "referer": "https://www.bhgre.com/home/list/county/nj/bergen-county",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        },
    }

    # New Jersey state placeId
    NJ_PLACE_ID = 'P02500000GAeUbeAzoJ1ps6gKlZmz08tVW67M95e'
    WARMUP_URL = "https://www.bhgre.com/home/list/county/nj/bergen-county"

    # API endpoints
    LISTINGS_API = 'https://www.bhgre.com/api/listings'
    LISTING_DETAIL_API_TEMPLATE = 'https://www.bhgre.com/api/listings/{listing_id}?ctxCode=BHG&showMlsListings=true'
    DEFAULT_API_KEY = "svbyT7C7Hw7d8D7GxJsi"

    # NJ Boundaries (approximate)
    # Northernmost point: ~41.35
    # Southernmost point: ~38.93
    # Westernmost point: ~74.75
    # Easternmost point: ~73.89
    NJ_BOUNDARY = {
        "topRightMapPoint": [-73.89, 41.35],
        "bottomLeftMapPoint": [-74.75, 38.93]
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = build_proxy_url()
        self.api_key = get_env("BHGRE_API_KEY", default=self.DEFAULT_API_KEY)
        self.max_pages = self._safe_int(
            kwargs.get("max_pages"),
            get_env("BHGRE_MAX_PAGES", default="0"),
            fallback=0,
        )

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    @staticmethod
    def _safe_int(value, default_value=None, fallback=None):
        candidate = value if value not in (None, "") else default_value
        try:
            return int(candidate) if candidate not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    async def start(self):
        """Generate initial requests for listings"""
        if self.proxy_url:
            self.logger.info("Using DataImpulse rotating proxy for BHGRE requests")
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
        """Prime session cookies and anti-bot state before hitting API."""
        self.logger.info("Warmup page status: %d", response.status)
        yield self.listings_request(1)

    def listings_request(self, page):
        """Create a request for listings API"""
        payload = {
            "ctx": {
                "brandCode": "BHG",
                "language": "en-US"
            },
            "numPerPage": 300,
            "page": page,
            "status": "ACTIVE,PENDING,COMING_SOON",
            "showMlsListings": True,
            "minNumImages": 0,
            "projectedFields": "projectedFields.UniversalPlatform",
            "placeMasterIds": self.NJ_PLACE_ID,
            "viewBoundary": self.NJ_BOUNDARY,
            "propertyType": "SFR,MFR,MFD,CONDO,TOWNHOUSE,COOP,LAND,FARM",
            "sortBy": '[{"key":"newListingTimeStamp","order":"DESC"}]'
        }

        return Request(
            url=self.LISTINGS_API,
            method='POST',
            body=json.dumps(payload),
            headers=self._api_headers(),
            callback=self.parse_listings,
            errback=self.handle_listings_error,
            meta={'page': page, **self._proxy_meta()}
        )

    def _api_headers(self):
        """Build required API headers for BHGRE listing requests."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.bhgre.com",
            "Referer": self.WARMUP_URL,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
            "Priority": "u=1, i",
            "x-api-key": self.api_key,
            "x-anywhere-id": str(uuid.uuid4()),
            "x-anywhere-request-id": str(uuid.uuid4()),
        }

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except AttributeError:
            return response.body.decode("utf-8", errors="replace")

    def parse_listings(self, response):
        """Parse the listings API response"""
        response_text = self._safe_response_text(response)
        if response.status == 401:
            self.logger.error(
                "Listings API returned 401 on page %s. Body preview: %s",
                response.meta.get("page"),
                response_text[:500],
            )
            return

        try:
            data = json.loads(response_text)
            listings = data.get('data', {}).get('results', [])
            pagination = data.get('data', {}).get('pagination', {})
            additional_info = data.get('data', {}).get('additionalInfo', {})

            # Yield each listing as an item
            for listing in listings:
                base_item = self.parse_listing_item(listing)
                listing_id = listing.get("id")
                if listing_id:
                    yield self.listing_detail_request(
                        listing_id=listing_id,
                        base_item=base_item,
                    )
                else:
                    yield base_item

            current_page = response.meta.get('page', 1)
            total_pages = self._derive_total_pages(
                pagination=pagination,
                additional_info=additional_info,
                current_page=current_page,
                current_results_count=len(listings),
            )
            if self.max_pages and self.max_pages > 0:
                total_pages = min(total_pages, self.max_pages)

            self.logger.info(
                "BHGRE page=%s listings=%s total_pages=%s total_results=%s page_size=%s",
                current_page,
                len(listings),
                total_pages,
                pagination.get("totalResults"),
                pagination.get("pageSize"),
            )

            if current_page < total_pages:
                yield self.listings_request(current_page + 1)

        except Exception as e:
            self.logger.error(f"Error parsing listings: {e}")

    def _derive_total_pages(self, pagination, additional_info, current_page, current_results_count):
        """Infer total pages across observed BHGRE pagination schema variants."""
        # Known variants observed across listing APIs.
        for source in (pagination or {}, additional_info or {}):
            for key in ("numOfPages", "totalPages", "numPages", "pages"):
                value = self._safe_int(source.get(key))
                if value and value > 0:
                    return value

        total_results = self._safe_int((pagination or {}).get("totalResults"))
        page_size = self._safe_int((pagination or {}).get("pageSize"))
        if total_results and page_size and page_size > 0:
            return max(1, int(math.ceil(total_results / float(page_size))))

        # Fallback if totals are missing: continue while page appears full.
        request_page_size = 300
        if current_results_count >= request_page_size:
            return current_page + 1
        return current_page

    def handle_listings_error(self, failure):
        request = getattr(failure, "request", None)
        self.logger.error("Listings request failed for %s: %s", getattr(request, "url", "unknown"), failure.value)

    def listing_detail_request(self, listing_id, base_item):
        return Request(
            url=self.LISTING_DETAIL_API_TEMPLATE.format(listing_id=listing_id),
            method='GET',
            headers=self._api_headers(),
            callback=self.parse_listing_detail,
            errback=self.handle_listing_detail_error,
            meta={"base_item": base_item, **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_listing_detail(self, response):
        base_item = dict(response.meta.get("base_item") or {})
        response_text = self._safe_response_text(response)
        if response.status != 200:
            self.logger.warning(
                "Listing detail API failed status=%s id=%s",
                response.status,
                base_item.get("id"),
            )
            yield base_item
            return

        try:
            data = json.loads(response_text)
            detail_listing = (data.get("data") or {}).get("result") or {}
            if not detail_listing:
                yield base_item
                return
            detail_item = self.parse_listing_item(detail_listing)
            yield self.merge_items(base_item, detail_item)
        except Exception as exc:
            self.logger.error(
                "Error parsing listing detail id=%s: %s",
                base_item.get("id"),
                exc,
            )
            yield base_item

    def handle_listing_detail_error(self, failure):
        request = getattr(failure, "request", None)
        base_item = dict((getattr(request, "meta", {}) or {}).get("base_item") or {})
        self.logger.error(
            "Listing detail request failed for %s: %s",
            getattr(request, "url", "unknown"),
            failure.value,
        )
        if base_item:
            return [base_item]
        return []

    @staticmethod
    def merge_items(base_item, detail_item):
        merged = dict(base_item or {})
        for key, value in (detail_item or {}).items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return merged

    @staticmethod
    def _join_value(value):
        if value in (None, "", [], {}):
            return None
        if isinstance(value, list):
            parts = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(parts) if parts else None
        if isinstance(value, dict):
            # Flatten lightweight dict values for simpler storage.
            parts = []
            for _, v in value.items():
                if isinstance(v, (str, int, float)) and str(v).strip():
                    parts.append(str(v).strip())
            return ", ".join(parts) if parts else None
        return value

    @staticmethod
    def _coalesce(*values):
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _get_in(source, path, default=None):
        cur = source
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur.get(key)
        return cur

    @staticmethod
    def _looks_like_phone(value):
        if value in (None, "", [], {}):
            return False
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return len(digits) >= 10

    def _extract_agent_contact(self, attribution):
        attribution = attribution or {}
        agent_name = attribution.get("agentName")
        agent_phone = attribution.get("phone")
        office_phone = attribution.get("officePhone")

        listing_agents = attribution.get("listingAgents") or []
        if isinstance(listing_agents, list):
            for agent in listing_agents:
                if not isinstance(agent, dict):
                    continue
                if not agent_name:
                    agent_name = self._coalesce(
                        agent.get("name"),
                        agent.get("agentName"),
                        agent.get("fullName"),
                    )
                if not agent_phone:
                    candidate = self._coalesce(
                        agent.get("phone"),
                        agent.get("mobilePhone"),
                        agent.get("cellPhone"),
                        agent.get("contactPhone"),
                    )
                    if self._looks_like_phone(candidate):
                        agent_phone = candidate
                if not office_phone:
                    candidate = self._coalesce(
                        agent.get("officePhone"),
                        agent.get("brokerPhone"),
                    )
                    if self._looks_like_phone(candidate):
                        office_phone = candidate
                if agent_name and agent_phone and office_phone:
                    break

        listing_teams = attribution.get("listingTeams") or []
        if isinstance(listing_teams, list):
            for team in listing_teams:
                if not isinstance(team, dict):
                    continue
                if not office_phone:
                    candidate = self._coalesce(
                        team.get("officePhone"),
                        team.get("phone"),
                        team.get("contactPhone"),
                    )
                    if self._looks_like_phone(candidate):
                        office_phone = candidate
                        break

        return agent_name, agent_phone, office_phone

    @staticmethod
    def _sanitize_raw_listing(listing):
        clean = copy.deepcopy(listing or {})
        mls = clean.get("mls")
        if isinstance(mls, dict):
            mls.pop("globalDisclaimer", None)
        return clean

    @staticmethod
    def _normalize_year_built(value):
        if value in (None, "", [], {}):
            return None
        try:
            year = int(value)
        except (TypeError, ValueError):
            return value
        current_year = datetime.now(timezone.utc).year
        if year > current_year + 2:
            return 0
        return year

    @staticmethod
    def _extract_photo_links(photos):
        if not isinstance(photos, dict):
            return []
        links = []
        seen = set()

        def add_link(value):
            if not isinstance(value, str):
                return
            candidate = value.strip()
            if not candidate or not candidate.startswith("http"):
                return
            if candidate in seen:
                return
            seen.add(candidate)
            links.append(candidate)

        add_link(photos.get("firstPhotoUrl"))
        media_items = photos.get("media") or []
        if isinstance(media_items, list):
            for media in media_items:
                if isinstance(media, dict):
                    add_link(media.get("mediaUrl"))
                    add_link(media.get("url"))
                    add_link(media.get("src"))
                elif isinstance(media, str):
                    add_link(media)
        return links

    def parse_listing_item(self, listing):
        """Extract property data from a single listing"""
        location = listing.get('location', {})
        property_info = listing.get('property', {})
        attribution = listing.get('attribution', {})
        area = listing.get('area', {})
        gis = listing.get('gis', {})
        price = listing.get('price', 0)
        characteristics = listing.get("characteristics", {})
        equipment = listing.get("equipment", {})
        utilities = listing.get("utilities", {})
        financial = listing.get("financial", {})
        photos = listing.get("photos", {})

        canonical_url = listing.get('canonicalURL')
        full_url = None
        if canonical_url:
            full_url = urljoin("https://www.bhgre.com", canonical_url)
        agent_name, agent_phone, office_phone = self._extract_agent_contact(attribution)
        photo_links = self._extract_photo_links(photos)

        item = {
            # Basic property info
            'id': listing.get('id'),
            'url': full_url or canonical_url,
            'price': price,
            'property_type': property_info.get('propertyType'),
            'status': property_info.get('listingStatus'),

            # Location info
            'address': location.get('unparsedAddress'),
            'city': location.get('city'),
            'state': location.get('stateCode'),
            'postal_code': location.get('postalCode'),
            'latitude': gis.get('latitude'),
            'longitude': gis.get('longitude'),

            # Property details
            'bedrooms': property_info.get('bedrooms'),
            'bathrooms': property_info.get('bathrooms'),
            'full_bathrooms': property_info.get('fullBathrooms'),
            'half_bathrooms': property_info.get('halfBathrooms'),
            'year_built': self._normalize_year_built(property_info.get('yearBuilt')),
            'listing_area': area.get('listingArea'),
            'lot_size': area.get('lotSize'),
            'lot_size_units': area.get('lotSizeUnits'),
            'living_area': area.get('livingArea'),
            'building_area': area.get('buildingArea'),
            'description': property_info.get("description"),
            'levels': property_info.get("levels"),
            'rooms_total': property_info.get("roomsTotal"),
            'architecture_style': property_info.get("architectureStyle"),
            'construction_materials': self._join_value(property_info.get("constructionMaterials")),
            'basement': property_info.get("basement"),
            'has_basement': property_info.get("hasBasement"),
            'foundation_details': self._join_value(property_info.get("foundationDetails")),
            'heating': self._coalesce(
                self._join_value(property_info.get("heating")),
                self._join_value(utilities.get("heating")),
            ),
            'cooling': self._coalesce(
                self._join_value(property_info.get("cooling")),
                self._join_value(utilities.get("cooling")),
            ),
            'flooring': self._join_value(property_info.get("flooring")),
            'appliances': self._join_value(equipment.get("appliances")),
            'lot_features': self._join_value(characteristics.get("lotFeatures")),
            'pool_features': self._join_value(characteristics.get("poolFeatures")),
            'laundry_features': self._join_value(characteristics.get("laundryFeatures")),
            'exterior_features': self._join_value(property_info.get("exteriorFeatures")),
            'parking_features': self._coalesce(
                self._join_value(property_info.get("parkingFeatures")),
                self._join_value(characteristics.get("parkingFeatures")),
            ),
            'parking_total': property_info.get("parkingTotal"),
            'other_parking_spaces': self._join_value(property_info.get("otherParkingSpaces")),
            'garage_spaces': property_info.get("garageSpaces"),
            'carport_spaces': property_info.get("carportSpaces"),
            'has_garage': property_info.get("hasGarage"),
            'has_central_air': property_info.get("hasCentralAir"),
            'days_on_market': listing.get("daysOnMarket"),
            'last_updated': listing.get("lastUpdated"),

            # MLS info
            'mls_id': listing.get('mls', {}).get('mlsNumber'),
            'mls_number': listing.get('mls', {}).get('mlsNumber'),
            'mls_source': listing.get('mls', {}).get('mlsShortName'),

            # Agent/Broker info
            'listing_office': attribution.get('listingOfficeName'),
            'agent_name': agent_name,
            'listing_office_phone': office_phone,
            'listing_agent_phone': agent_phone,

            # Photo info
            'photos_count': photos.get('photosCount', 0),
            'first_photo_url': self._coalesce(photos.get('firstPhotoUrl'), photo_links[0] if photo_links else None),
            'photo_links': photo_links,

            # Open houses
            'open_houses': listing.get('openHouses', []),

            # Additional flags
            'is_luxury': listing.get('isLuxuryListing', False),
            'has_images': len(photo_links) > 0,
            'tax_annual_amount': financial.get("taxAnnualAmount"),
            'tax_year': financial.get("taxYear"),
            'hoa_dues_monthly': financial.get("hoaDuesMonthly"),
            'amenities': self._join_value(listing.get("amenities")),

            # Raw data
            'raw_listing': self._sanitize_raw_listing(listing)
        }

        return item
