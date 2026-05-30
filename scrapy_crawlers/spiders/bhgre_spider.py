import math
import scrapy
import json
import uuid
import copy
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin
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
    PLACES_API = "https://www.bhgre.com/api/places"
    NEIGHBOR_PLACES_API_TEMPLATE = "https://www.bhgre.com/api/neighborPlaces/{place_master_id}"
    LISTING_DETAIL_API_TEMPLATE = 'https://www.bhgre.com/api/listings/{listing_id}?ctxCode=BHG&showMlsListings=true'
    DEFAULT_API_KEY = "svbyT7C7Hw7d8D7GxJsi"

    # Full NJ extent: [lon, lat] pairs — topRight = NE corner, bottomLeft = SW corner
    NJ_BOUNDARY = {
        "topRightMapPoint": [-73.88, 41.36],
        "bottomLeftMapPoint": [-75.57, 38.78],
    }

    # BHGRE caps results at 300 per request regardless of pagination params.
    # When a query hits the cap, we split the viewBoundary into 4 quadrants
    # and recurse. MAX_BBOX_DEPTH prevents infinite recursion in dense areas;
    # at depth 14 each tile is ~100 m × ~100 m — finer than any property cluster.
    LISTINGS_CAP = 300
    MAX_BBOX_DEPTH = 14

    STAGE_ZIP = "zip"
    STAGE_CITY = "city"
    STAGE_BBOX = "bbox"
    PLACE_TYPES_BY_STAGE = {
        STAGE_ZIP: "postalCode",
        STAGE_CITY: "city",
    }
    PHOTO_INDEX_PATTERN = re.compile(r"^(?P<prefix>.*?_P)(?P<idx>\d{2})(?P<suffix>\.[A-Za-z0-9]+(?:\?.*)?)$")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = build_proxy_url()
        self.api_key = get_env("BHGRE_API_KEY", default=self.DEFAULT_API_KEY)
        self.max_pages = self._safe_int(
            kwargs.get("max_pages"),
            get_env("BHGRE_MAX_PAGES", default="0"),
            fallback=0,
        )
        self.place_num_per_page = self._safe_int(
            kwargs.get("place_num_per_page"),
            get_env("BHGRE_PLACE_NUM_PER_PAGE", default="200"),
            fallback=200,
        ) or 200
        self.max_place_pages = self._safe_int(
            kwargs.get("max_place_pages"),
            get_env("BHGRE_MAX_PLACE_PAGES", default="0"),
            fallback=0,
        ) or 0
        self.enable_tiered_place_search = str(
            self._coalesce(
                kwargs.get("enable_tiered_place_search"),
                get_env("BHGRE_ENABLE_TIERED_PLACE_SEARCH", default="1"),
            )
        ).strip() not in {"0", "false", "False", "no", "No"}
        self.seen_listing_ids = set()
        self.seen_place_canonicals = set()
        self.bbox_started = False

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
        if self.enable_tiered_place_search:
            self.logger.info("BHGRE staged strategy enabled: ZIP -> city -> bbox fallback")
            req = self.neighbor_places_request(stage=self.STAGE_ZIP, page=1)
            if req is not None:
                yield req
            return
        yield self.listings_request(shard=self._bbox_shard())

    def listings_request(self, shard):
        """Create a listings API request for a geographic shard."""
        payload = self._build_listings_payload(shard=shard)
        referer = self._canonical_to_referer((shard or {}).get("canonical_url"))
        return Request(
            url=self.LISTINGS_API,
            method='POST',
            body=json.dumps(payload),
            headers=self._api_headers(referer=referer),
            callback=self.parse_listings,
            errback=self.handle_listings_error,
            meta={
                'shard': shard,
                **self._proxy_meta(),
            },
        )

    def _build_listings_payload(self, shard):
        shard = shard or {}
        place_master_id = shard.get("place_master_id")
        # Always send a viewBoundary — it is the only way to paginate BHGRE.
        # Fall back to full NJ extent when the shard has no specific boundary.
        boundary = shard.get("boundary") or self.NJ_BOUNDARY
        return {
            "ctx": {"brandCode": "BHG", "language": "en-US"},
            "numPerPage": self.LISTINGS_CAP,
            "status": "ACTIVE,PENDING,COMING_SOON",
            "showMlsListings": True,
            "minNumImages": 0,
            "projectedFields": "projectedFields.UniversalPlatform",
            "placeMasterIds": place_master_id or self.NJ_PLACE_ID,
            "viewBoundary": boundary,
            "propertyType": "SFR,MFR,MFD,CONDO,TOWNHOUSE,COOP,LAND,FARM",
            "sortBy": '[{"key":"newListingTimeStamp","order":"DESC"}]',
        }

    def neighbor_places_request(self, stage, page):
        place_type = self.PLACE_TYPES_BY_STAGE.get(stage)
        if not place_type:
            return None
        params = {
            "brand": "BHG",
            "placeType": place_type,
            "applyListingsFilter": "true",
            "page": page,
            "numPerPage": self.place_num_per_page,
        }
        url = (
            self.NEIGHBOR_PLACES_API_TEMPLATE.format(place_master_id=self.NJ_PLACE_ID)
            + "?"
            + urlencode(params)
        )
        return Request(
            url=url,
            method="GET",
            headers=self._api_headers(referer=self.WARMUP_URL),
            callback=self.parse_neighbor_places,
            errback=self.handle_neighbor_places_error,
            meta={
                "stage": stage,
                "place_type": place_type,
                "place_page": page,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def place_request_by_canonical(self, stage, canonical_url):
        if not canonical_url:
            return None
        params = {
            "brand": "BHG",
            "canonicalUrl": canonical_url,
        }
        url = self.PLACES_API + "?" + urlencode(params)
        return Request(
            url=url,
            method="GET",
            headers=self._api_headers(referer=self._canonical_to_referer(canonical_url)),
            callback=self.parse_place,
            errback=self.handle_place_error,
            meta={
                "stage": stage,
                "canonical_url": canonical_url,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def _api_headers(self, referer=None):
        """Build required API headers for BHGRE listing requests."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.bhgre.com",
            "Referer": referer or self.WARMUP_URL,
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

    def parse_neighbor_places(self, response):
        stage = response.meta.get("stage")
        place_page = self._safe_int(response.meta.get("place_page"), fallback=1) or 1
        data = self._parse_json_response(response, context=f"neighbor_places_{stage}_p{place_page}")
        if data is None:
            self._advance_stage(stage)
            return

        payload = data.get("data") or {}
        results = payload.get("results") or []
        pagination = payload.get("pagination") or {}
        seeded = 0
        for row in results:
            if not isinstance(row, dict):
                continue
            canonical_url = row.get("canonicalUrl")
            if not self._is_nj_canonical(canonical_url):
                continue
            canonical_key = str(canonical_url).strip().lower()
            if canonical_key in self.seen_place_canonicals:
                continue
            self.seen_place_canonicals.add(canonical_key)
            req = self.place_request_by_canonical(stage=stage, canonical_url=canonical_url)
            if req is not None:
                seeded += 1
                yield req

        total_pages = self._infer_total_pages(
            pagination=pagination,
            current_page=place_page,
            current_results_count=len(results),
            page_size=self.place_num_per_page,
        )
        if self.max_place_pages and self.max_place_pages > 0:
            total_pages = min(total_pages, self.max_place_pages)

        self.logger.info(
            "BHGRE %s seed page=%s seeded=%s results=%s total_pages=%s",
            stage,
            place_page,
            seeded,
            len(results),
            total_pages,
        )

        if place_page < total_pages:
            next_req = self.neighbor_places_request(stage=stage, page=place_page + 1)
            if next_req is not None:
                yield next_req
            return

        self._advance_stage(stage)
        if stage == self.STAGE_ZIP:
            req = self.neighbor_places_request(stage=self.STAGE_CITY, page=1)
            if req is not None:
                yield req
            return
        if stage == self.STAGE_CITY:
            yield from self.start_bbox_fallback_if_needed()

    def parse_place(self, response):
        stage = response.meta.get("stage")
        canonical_url = response.meta.get("canonical_url")
        data = self._parse_json_response(response, context=f"place_{stage}_{canonical_url}")
        if data is None:
            return
        results = (data.get("data") or {}).get("results") or []
        if not results:
            return
        place = results[0] if isinstance(results[0], dict) else {}
        place_master_id = place.get("placeMasterId")
        if not place_master_id:
            return
        shard = {
            "stage": stage,
            "shard_key": f"{stage}:{canonical_url}",
            "canonical_url": canonical_url,
            "display_name": place.get("displayName") or place.get("placeName") or canonical_url,
            "place_master_id": place_master_id,
            "boundary": self._extract_boundary_from_place(place),
            "bbox_depth": 0,
        }
        yield self.listings_request(shard=shard)

    def start_bbox_fallback_if_needed(self):
        if self.bbox_started:
            return
        self.bbox_started = True
        self.logger.info("BHGRE starting bbox fallback shard for NJ")
        yield self.listings_request(shard=self._bbox_shard())

    def parse_listings(self, response):
        """Parse the listings API response and recurse via bbox subdivision if capped."""
        response_text = self._safe_response_text(response)
        shard = response.meta.get("shard") or {}
        shard_key = shard.get("shard_key", self.STAGE_BBOX)

        if response.status == 401:
            self.logger.error(
                "Listings API 401 shard=%s body_preview=%s",
                shard_key,
                response_text[:500],
            )
            return

        try:
            data = json.loads(response_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.logger.error(
                "Listings JSON parse failed shard=%s status=%s body_preview=%s",
                shard_key,
                response.status,
                response_text[:500],
            )
            return

        listings = (data.get("data") or {}).get("results") or []
        pagination = (data.get("data") or {}).get("pagination") or {}
        total_results = pagination.get("totalResults")

        emitted = 0
        for listing in listings:
            listing_id = listing.get("id")
            if listing_id and listing_id in self.seen_listing_ids:
                continue
            if listing_id:
                self.seen_listing_ids.add(listing_id)
            base_item = self.parse_listing_item(listing)
            emitted += 1
            if listing_id:
                yield self.listing_detail_request(listing_id=listing_id, base_item=base_item)
            else:
                yield self._as_property_item(base_item)

        bbox_depth = shard.get("bbox_depth") or 0
        hit_cap = len(listings) >= self.LISTINGS_CAP

        self.logger.info(
            "BHGRE shard=%s depth=%s results=%s emitted=%s total_results=%s cap_hit=%s unique_seen=%s",
            shard_key,
            bbox_depth,
            len(listings),
            emitted,
            total_results,
            hit_cap,
            len(self.seen_listing_ids),
        )

        if not hit_cap:
            return

        if bbox_depth >= self.MAX_BBOX_DEPTH:
            self.logger.warning(
                "BHGRE shard=%s hit max bbox depth %d with %d results; cannot subdivide further",
                shard_key,
                bbox_depth,
                len(listings),
            )
            return

        # Split the current bounding box into 4 quadrants and re-query each.
        boundary = shard.get("boundary") or self.NJ_BOUNDARY
        for sub_boundary in self._split_bbox(boundary):
            tr = sub_boundary["topRightMapPoint"]
            sub_shard = {
                **shard,
                "boundary": sub_boundary,
                "bbox_depth": bbox_depth + 1,
                "shard_key": f"{shard_key}|d{bbox_depth+1}@{tr[0]:.4f},{tr[1]:.4f}",
            }
            yield self.listings_request(shard=sub_shard)

    @staticmethod
    def _split_bbox(boundary):
        """Split a bounding box into 4 equal quadrants (NE, NW, SE, SW).

        boundary uses [lon, lat] pairs:
          topRightMapPoint  = NE corner = [east_lon, north_lat]
          bottomLeftMapPoint = SW corner = [west_lon, south_lat]
        """
        tr_lon, tr_lat = boundary["topRightMapPoint"]
        bl_lon, bl_lat = boundary["bottomLeftMapPoint"]
        mid_lon = (tr_lon + bl_lon) / 2.0
        mid_lat = (tr_lat + bl_lat) / 2.0
        return [
            {"topRightMapPoint": [tr_lon,  tr_lat],  "bottomLeftMapPoint": [mid_lon, mid_lat]},  # NE
            {"topRightMapPoint": [mid_lon, tr_lat],  "bottomLeftMapPoint": [bl_lon,  mid_lat]},  # NW
            {"topRightMapPoint": [tr_lon,  mid_lat], "bottomLeftMapPoint": [mid_lon, bl_lat]},   # SE
            {"topRightMapPoint": [mid_lon, mid_lat], "bottomLeftMapPoint": [bl_lon,  bl_lat]},   # SW
        ]

    def handle_listings_error(self, failure):
        request = getattr(failure, "request", None)
        shard_key = ((getattr(request, "meta", {}) or {}).get("shard") or {}).get("shard_key")
        self.logger.error(
            "Listings request failed for %s shard=%s: %s",
            getattr(request, "url", "unknown"),
            shard_key,
            failure.value,
        )

    def handle_neighbor_places_error(self, failure):
        request = getattr(failure, "request", None)
        meta = getattr(request, "meta", {}) or {}
        self.logger.error(
            "Neighbor places request failed stage=%s page=%s url=%s err=%s",
            meta.get("stage"),
            meta.get("place_page"),
            getattr(request, "url", "unknown"),
            failure.value,
        )

    def handle_place_error(self, failure):
        request = getattr(failure, "request", None)
        meta = getattr(request, "meta", {}) or {}
        self.logger.error(
            "Place lookup failed stage=%s canonical=%s url=%s err=%s",
            meta.get("stage"),
            meta.get("canonical_url"),
            getattr(request, "url", "unknown"),
            failure.value,
        )

    @staticmethod
    def _infer_total_pages(pagination, current_page, current_results_count, page_size):
        """Infer total pages from the neighborPlaces pagination block."""
        for key in ("numOfPages", "totalPages", "numPages", "pages"):
            value = BhgreSpider._safe_int(pagination.get(key))
            if value and value > 0:
                return value
        total_results = BhgreSpider._safe_int(pagination.get("totalResults"))
        if total_results and page_size and page_size > 0:
            return max(1, math.ceil(total_results / float(page_size)))
        if current_results_count >= page_size:
            return current_page + 1
        return current_page

    def _advance_stage(self, stage):
        if stage == self.STAGE_ZIP:
            self.logger.info("BHGRE ZIP seed stage completed")
        elif stage == self.STAGE_CITY:
            self.logger.info("BHGRE city seed stage completed")

    def _bbox_shard(self):
        return {
            "stage": self.STAGE_BBOX,
            "shard_key": self.STAGE_BBOX,
            "canonical_url": "/state/nj",
            "display_name": "New Jersey (bbox)",
            "place_master_id": self.NJ_PLACE_ID,
            "boundary": self.NJ_BOUNDARY,
            "bbox_depth": 0,
        }

    @staticmethod
    def _canonical_to_referer(canonical_url):
        c = str(canonical_url or "").strip()
        if not c:
            return "https://www.bhgre.com/home/list/state/nj"
        if c.startswith("http://") or c.startswith("https://"):
            return c
        return "https://www.bhgre.com/home/list" + c

    @staticmethod
    def _extract_boundary_from_place(place):
        if not isinstance(place, dict):
            return None
        bottom_left = place.get("bottomLeftBoundingBox")
        top_right = place.get("topRightBoundingBox")
        if (
            isinstance(bottom_left, list)
            and len(bottom_left) == 2
            and isinstance(top_right, list)
            and len(top_right) == 2
        ):
            return {
                "topRightMapPoint": [top_right[0], top_right[1]],
                "bottomLeftMapPoint": [bottom_left[0], bottom_left[1]],
            }
        return None

    @staticmethod
    def _is_nj_canonical(canonical_url):
        return "/nj/" in str(canonical_url or "").lower()

    def _parse_json_response(self, response, context):
        text = self._safe_response_text(response)
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.logger.error(
                "BHGRE %s JSON parse failed status=%s body_preview=%s",
                context,
                response.status,
                text[:500],
            )
            return None

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
            yield self._as_property_item(base_item)
            return

        try:
            data = json.loads(response_text)
            detail_listing = (data.get("data") or {}).get("result") or {}
            if not detail_listing:
                yield self._as_property_item(base_item)
                return
            detail_item = self.parse_listing_item(detail_listing)
            yield self._as_property_item(self.merge_items(base_item, detail_item))
        except Exception as exc:
            self.logger.error(
                "Error parsing listing detail id=%s: %s",
                base_item.get("id"),
                exc,
            )
            yield self._as_property_item(base_item)

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
    def _as_property_item(document):
        return dict(document or {})

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
        # Fallback: some payloads provide only firstPhotoUrl + photosCount.
        # Expand sequential URLs where possible so we do not lose gallery breadth.
        first_photo = photos.get("firstPhotoUrl")
        photos_count = BhgreSpider._safe_int(photos.get("photosCount"), default_value=None, fallback=None)
        if first_photo and photos_count and photos_count > 1 and len(links) <= 1:
            for url in BhgreSpider._expand_photo_sequence(first_photo, photos_count):
                add_link(url)
        return links

    @staticmethod
    def _expand_photo_sequence(first_photo_url, photos_count):
        if not isinstance(first_photo_url, str):
            return []
        match = BhgreSpider.PHOTO_INDEX_PATTERN.match(first_photo_url.strip())
        if not match:
            return [first_photo_url]
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        urls = []
        for idx in range(max(1, int(photos_count))):
            urls.append(f"{prefix}{idx:02d}{suffix}")
        return urls

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

        # Keep count consistent when payload count is missing/inaccurate.
        if (item.get("photos_count") in (None, 0)) and photo_links:
            item["photos_count"] = len(photo_links)

        return item
