import json
import uuid
from copy import deepcopy

import scrapy

from spiders.env_config import build_proxy_url

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


class RealtorSpider(scrapy.Spider):
    name = "realtor"
    allowed_domains = ["www.realtor.com"]
    handle_httpstatus_list = [400, 401, 403, 429]
    custom_settings = {
        "CURL_IMPERSONATE": "chrome110",
    }

    SEARCH_URL = "https://www.realtor.com/realestateandhomes-search/New-Jersey"
    GRAPHQL_URL = "https://www.realtor.com/frontdoor/graphql"
    PAGE_SIZE = 42

    GRAPHQL_QUERY = """
query ConsumerSearchQuery($query: HomeSearchCriteria!, $limit: Int, $offset: Int, $search_promotion: SearchPromotionInput, $sort: [SearchAPISort], $sort_type: SearchSortType, $client_data: JSON, $bucket: SearchAPIBucket, $mortgage_params: MortgageParamsInput, $photosLimit: Int) {
  home_search: home_search(
    query: $query
    sort: $sort
    limit: $limit
    offset: $offset
    sort_type: $sort_type
    client_data: $client_data
    bucket: $bucket
    search_promotion: $search_promotion
    mortgage_params: $mortgage_params
  ) {
    count
    total
    search_promotion {
      names
      slots
      promoted_properties {
        id
        from_other_page
      }
    }
    mortgage_params {
      interest_rate
    }
    properties: results {
      property_id
      list_price
      rmn_listing_attribution
      search_promotions {
        name
        asset_id
      }
      primary_photo(https: true) {
        href
      }
      listing_id
      matterport
      virtual_tours {
        href
      }
      status
      products {
        products
        brand_name
      }
      source {
        id
        name
        type
        spec_id
        plan_id
        listing_id
      }
      lead_attributes {
        show_contact_an_agent
        market_type
        lead_type
        is_veterans_united_eligible
      }
      community {
        description {
          name
        }
        property_id
        permalink
        advertisers {
          office {
            hours
            phones {
              type
              number
              primary
              trackable
            }
          }
        }
        promotions {
          description
          href
          headline
          promotion_type
        }
      }
      permalink
      price_reduced_amount
      description {
        name
        beds
        baths_consolidated
        sqft
        lot_sqft
        baths_max
        baths_min
        beds_min
        beds_max
        sqft_min
        sqft_max
        type
        sub_type
        sold_price
        sold_date
        year_built
        garage
      }
      location {
        street_view_url
        address {
          line
          postal_code
          state
          state_code
          city
          coordinate {
            lat
            lon
          }
        }
        county {
          name
          fips_code
        }
      }
      open_houses {
        start_date
        end_date
        description
        time_zone
        dst
      }
      branding {
        type
        name
        photo
      }
      flags {
        is_coming_soon
        is_new_listing(days: 14)
        is_price_reduced(days: 30)
        is_foreclosure
        is_new_construction
        is_pending
        is_contingent
        is_auction
        is_fractionally_owned
        is_non_deeded
      }
      list_date
      photo_count
      photos(limit: $photosLimit, https: true) {
        href
      }
      advertisers {
        type
        fulfillment_id
        name
        builder {
          name
          href
          logo
          fulfillment_id
        }
        email
        office {
          name
        }
        phones {
          number
        }
      }
    }
    sort_model
    experiment {
      experiment_name
      experiment_variant
      experiment_key
    }
  }
  commute_polygon: get_commute_polygon(query: $query) {
    areas {
      id
      breakpoints {
        width
        height
        zoom
      }
      radius
      center {
        lat
        lng
      }
    }
    boundary
  }
}
""".strip()

    BASE_VARIABLES = {
        "photosLimit": 3,
        "query": {
            "primary": True,
            "status": ["for_sale", "ready_to_build"],
            "search_location": {"location": "New Jersey"},
        },
        "client_data": {"device_data": {"device_type": "desktop"}},
        "sort_type": "relevant",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = build_proxy_url()
        self.seen_property_ids = set()

    def _proxy_meta(self):
        if not self.proxy_url:
            return {}
        return {"proxy": self.proxy_url}

    async def start(self):
        visitor_id = str(uuid.uuid4())
        if self.proxy_url:
            self.logger.info("Using rotating proxy for Realtor requests")
        else:
            self.logger.warning(
                "Proxy env vars are not fully configured; running without a proxy"
            )
        yield scrapy.Request(
            self.SEARCH_URL,
            callback=self.parse_warmup,
            errback=self.handle_warmup_error,
            headers=self._graphql_headers(visitor_id, self.SEARCH_URL),
            meta={"visitor_id": visitor_id, "dont_retry": True, **self._proxy_meta()},
            dont_filter=True,
        )

    def parse_warmup(self, response):
        visitor_id = response.meta["visitor_id"]
        self.logger.info("Warmup page status: %d", response.status)
        for county in NJ_COUNTIES:
            search_location = f"{county} County, NJ"
            yield self.search_request(
                offset=0,
                visitor_id=visitor_id,
                search_location=search_location,
            )

    def handle_warmup_error(self, failure):
        request = getattr(failure, "request", None)
        visitor_id = request.meta.get("visitor_id") if request else str(uuid.uuid4())
        self.logger.warning("Warmup failed (%s). Continuing with GraphQL search request.", failure.value)
        for county in NJ_COUNTIES:
            search_location = f"{county} County, NJ"
            yield self.search_request(
                offset=0,
                visitor_id=visitor_id,
                search_location=search_location,
            )

    def _graphql_headers(self, visitor_id, referer):
        return {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "dnt": "1",
            "origin": "https://www.realtor.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "rdc-ab-test-client": "rdc-search-for-sale",
            "rdc-client-name": "RDC_WEB_SRP_FS_PAGE",
            "rdc-client-version": "3.0.2798",
            "referer": referer,
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-is-bot": "false",
            "x-rdc-visitor-id": visitor_id,
        }

    def search_request(self, offset, visitor_id, search_location):
        page_num = (offset // self.PAGE_SIZE) + 1
        referer = self.SEARCH_URL if page_num == 1 else f"{self.SEARCH_URL}/pg-{page_num}"
        variables = deepcopy(self.BASE_VARIABLES)
        variables["query"]["search_location"]["location"] = search_location
        variables["limit"] = self.PAGE_SIZE
        variables["offset"] = offset
        payload = {
            "operationName": "ConsumerSearchQuery",
            "variables": variables,
            "query": self.GRAPHQL_QUERY,
        }
        return scrapy.Request(
            url=self.GRAPHQL_URL,
            method="POST",
            body=json.dumps(payload),
            headers=self._graphql_headers(visitor_id, referer),
            callback=self.parse_search_results,
            errback=self.handle_search_error,
            meta={
                "offset": offset,
                "visitor_id": visitor_id,
                "search_location": search_location,
                **self._proxy_meta(),
            },
            dont_filter=True,
        )

    def parse_search_results(self, response):
        response_text = self._safe_response_text(response)
        if response.status != 200:
            self.logger.error(
                "GraphQL search failed. status=%s offset=%s body=%s",
                response.status,
                response.meta.get("offset"),
                response_text[:500],
            )
            return

        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            self.logger.error("Failed to parse GraphQL JSON: %s", exc)
            return

        home_search = payload.get("data", {}).get("home_search", {})
        properties = home_search.get("properties") or []
        count = home_search.get("count") or len(properties)
        total = home_search.get("total") or 0
        offset = response.meta.get("offset", 0)
        search_location = response.meta.get("search_location")
        self.logger.info(
            "Realtor shard=%s offset=%s count=%s total=%s",
            search_location,
            offset,
            count,
            total,
        )

        for prop in properties:
            unique_id = prop.get("property_id") or prop.get("listing_id")
            if unique_id:
                unique_id = str(unique_id)
                if unique_id in self.seen_property_ids:
                    continue
                self.seen_property_ids.add(unique_id)
            yield self.parse_property(prop, search_location=search_location)

        next_offset = offset + count
        if properties and next_offset < total:
            yield self.search_request(
                offset=next_offset,
                visitor_id=response.meta["visitor_id"],
                search_location=search_location,
            )

    @staticmethod
    def _safe_response_text(response):
        try:
            return response.text
        except AttributeError:
            return response.body.decode("utf-8", errors="replace")

    def handle_search_error(self, failure):
        request = getattr(failure, "request", None)
        self.logger.error("GraphQL request failed for %s: %s", getattr(request, "url", "unknown"), failure.value)

    @staticmethod
    def parse_property(prop, search_location):
        desc = prop.get("description") or {}
        location = prop.get("location") or {}
        if not isinstance(desc, dict):
            desc = {}
        if not isinstance(location, dict):
            location = {}

        address = location.get("address") or {}
        coord = address.get("coordinate") or {}
        county = location.get("county") or {}
        if not isinstance(address, dict):
            address = {}
        if not isinstance(coord, dict):
            coord = {}
        if not isinstance(county, dict):
            county = {}
        source = prop.get("source", {})
        primary_photo = prop.get("primary_photo", {})
        photos = prop.get("photos") or []
        first_branding = (prop.get("branding") or [{}])[0]
        first_advertiser = (prop.get("advertisers") or [{}])[0]
        first_office = first_advertiser.get("office", {}) if isinstance(first_advertiser, dict) else {}
        phones = first_advertiser.get("phones") if isinstance(first_advertiser, dict) else []
        if not isinstance(phones, list):
            phones = []

        return {
            "property_id": prop.get("property_id"),
            "listing_id": prop.get("listing_id"),
            "url": f"https://www.realtor.com/realestateandhomes-detail/{prop.get('permalink')}" if prop.get("permalink") else None,
            "status": prop.get("status"),
            "list_price": prop.get("list_price"),
            "price_reduced_amount": prop.get("price_reduced_amount"),
            "list_date": prop.get("list_date"),
            "beds": desc.get("beds"),
            "baths": desc.get("baths_consolidated"),
            "sqft": desc.get("sqft"),
            "lot_sqft": desc.get("lot_sqft"),
            "property_type": desc.get("type"),
            "property_sub_type": desc.get("sub_type"),
            "year_built": desc.get("year_built"),
            "address": address.get("line"),
            "city": address.get("city"),
            "state": address.get("state_code"),
            "postal_code": address.get("postal_code"),
            "county": county.get("name"),
            "latitude": coord.get("lat"),
            "longitude": coord.get("lon"),
            "source_mls": source.get("id"),
            "source_listing_id": source.get("listing_id"),
            "photo_count": prop.get("photo_count"),
            "primary_photo_url": primary_photo.get("href"),
            "photo_urls": [p.get("href") for p in photos if p.get("href")],
            "brand_name": first_branding.get("name"),
            "agent_name": first_advertiser.get("name") if isinstance(first_advertiser, dict) else None,
            "agent_office": first_office.get("name") if isinstance(first_office, dict) else None,
            "agent_phones": [p.get("number") for p in phones if p.get("number")],
            "open_houses": prop.get("open_houses") or [],
            "search_location": search_location,
            "raw_property": prop,
        }
