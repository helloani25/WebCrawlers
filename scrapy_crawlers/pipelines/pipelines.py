import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scrapy.exceptions import DropItem

from items import PropertyItem
from spiders.env_config import get_env


class DriftArtifactJsonLinesPipeline:
    """Append drift artifacts to a local JSONL file and keep them out of normal item sinks."""

    ARTIFACT_TYPE = "failed_card_drift"

    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.file_handles = {}

    @classmethod
    def from_crawler(cls, crawler):
        output_dir = get_env("DRIFT_ARTIFACTS_DIR", default="drift_artifacts")
        return cls(output_dir=output_dir)

    def open_spider(self, spider):
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def close_spider(self, spider):
        handle = self.file_handles.pop(spider.name, None)
        if handle:
            handle.close()

    def process_item(self, item, spider):
        if item.get("__artifact_type__") != self.ARTIFACT_TYPE:
            return item

        handle = self._file_handle_for_spider(spider.name)
        payload = dict(item)
        payload.setdefault("spider", spider.name)
        payload.setdefault("written_at", datetime.now(timezone.utc).isoformat())
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
        handle.flush()
        raise DropItem("Drift artifact stored in local JSONL")

    def _file_handle_for_spider(self, spider_name):
        handle = self.file_handles.get(spider_name)
        if handle:
            return handle

        path = self.output_dir / f"{spider_name}_failed_cards.jsonl"
        handle = path.open("a", encoding="utf-8")
        self.file_handles[spider_name] = handle
        return handle


class PropertyNormalizationPipeline:
    """Normalize spider-specific property keys into a shared canonical schema."""

    CANONICAL_SCHEMA_VERSION = 6

    CANONICAL_FIELDS = (
        "source",
        "source_listing_id",
        "mls_id",
        "detail_url",
        "address",
        "city",
        "state",
        "postal_code",
        "county",
        "latitude",
        "longitude",
        "list_price",
        "status",
        "property_type",
        "property_sub_type",
        "beds",
        "baths",
        "full_baths",
        "half_baths",
        "sqft",
        "living_area_sqft",
        "lot_size_sqft",
        "lot_size_acres",
        "year_built",
        "stories",
        "days_on_market",
        "garage_spaces",
        "heating",
        "cooling",
        "construction_materials",
        "foundation_details",
        "exterior_features",
        "tax_annual_amount",
        "tax_year",
        "description",
        "listing_agent",
        "listing_office",
        "listing_office_phone",
        "photos_count",
        "first_photo_url",
        "photo_links",
    )

    ALIASES = {
        "source": ("source",),
        "source_listing_id": ("source_listing_id", "listing_id", "id", "zpid", "property_id", "pals_id", "sys_id"),
        "mls_id": ("mls_id", "mls_number", "source_mls"),
        "detail_url": ("detail_url", "url"),
        "address": ("address", "street"),
        "city": ("city", "town"),
        "state": ("state",),
        "postal_code": ("postal_code",),
        "county": ("county",),
        "latitude": ("latitude", "lat"),
        "longitude": ("longitude", "lon"),
        "list_price": ("list_price", "price"),
        "status": ("status", "status_text"),
        "property_type": ("property_type", "home_type"),
        "property_sub_type": ("property_sub_type",),
        "beds": ("beds", "bedrooms", "bedrooms_total"),
        "baths": ("baths", "bathrooms", "bathrooms_total", "total_baths"),
        "full_baths": ("full_baths", "full_bathrooms", "bathrooms_full"),
        "half_baths": ("half_baths", "half_bathrooms", "bathrooms_half"),
        "sqft": ("sqft", "build_area_sqft", "listing_area", "building_area"),
        "living_area_sqft": ("living_area_sqft", "living_area"),
        "lot_size_sqft": ("lot_size_sqft", "lot_sqft"),
        "lot_size_acres": ("lot_size_acres",),
        "year_built": ("year_built",),
        "stories": ("stories",),
        "days_on_market": ("days_on_market", "days_on_website", "days_on_zillow"),
        "garage_spaces": ("garage_spaces", "garage", "carport_spaces"),
        "heating": ("heating", "heat_system", "heat_source", "heat_cool"),
        "cooling": ("cooling", "cool_system"),
        "construction_materials": ("construction_materials",),
        "foundation_details": ("foundation_details", "foundation_desc"),
        "exterior_features": ("exterior_features", "exterior"),
        "tax_annual_amount": ("tax_annual_amount", "tax_amount", "taxes"),
        "tax_year": ("tax_year", "tax_rate_year"),
        "description": ("description",),
        "listing_agent": ("listing_agent", "agent_name"),
        "listing_office": ("listing_office", "agent_office", "broker_name"),
        "listing_office_phone": ("listing_office_phone", "office_phone"),
        "photos_count": ("photos_count", "photo_count"),
        "first_photo_url": ("first_photo_url", "primary_photo_url"),
        "photo_links": ("photo_links", "photo_urls"),
    }

    def process_item(self, item, spider):
        if item.get("__artifact_type__"):
            return item

        document = dict(item)

        # Normalize canonical keys while preserving source-specific fields.
        for canonical_key in self.CANONICAL_FIELDS:
            value = self._first_present(document, *self.ALIASES.get(canonical_key, (canonical_key,)))
            if value in (None, "", []):
                continue
            document[canonical_key] = value

        # Enrich a few cross-spider fields from common fallbacks.
        if document.get("detail_url") in (None, "") and document.get("url") not in (None, ""):
            document["detail_url"] = document["url"]
        if document.get("listing_office_phone") in (None, ""):
            listing_phone = self._first_present(document, "listing_agent_phone")
            if listing_phone not in (None, "", []):
                document["listing_office_phone"] = listing_phone

        # Redfin-style structured/serialized status -> display value.
        document["status"] = self._normalize_status(document.get("status"))
        self._enrich_zillow_from_raw(document)

        # Canonical type coercion.
        document["latitude"] = self._to_float(document.get("latitude"))
        document["longitude"] = self._to_float(document.get("longitude"))
        document["list_price"] = self._to_int(document.get("list_price"))
        document["beds"] = self._to_float(document.get("beds"))
        document["baths"] = self._to_float(document.get("baths"))
        document["full_baths"] = self._to_int(document.get("full_baths"))
        document["half_baths"] = self._to_int(document.get("half_baths"))
        document["sqft"] = self._to_int(document.get("sqft"))
        document["living_area_sqft"] = self._to_int(document.get("living_area_sqft"))
        document["lot_size_sqft"] = self._to_int(document.get("lot_size_sqft"))
        document["lot_size_acres"] = self._to_float(document.get("lot_size_acres"))
        document["year_built"] = self._to_int(document.get("year_built"))
        document["stories"] = self._to_float(document.get("stories"))
        document["days_on_market"] = self._to_int(document.get("days_on_market"))
        document["garage_spaces"] = self._to_float(document.get("garage_spaces"))
        document["tax_annual_amount"] = self._to_int(document.get("tax_annual_amount"))
        document["tax_year"] = self._to_int(document.get("tax_year"))
        document["photos_count"] = self._to_int(document.get("photos_count"))

        if document.get("baths") is None and document.get("full_baths") is not None:
            half = document.get("half_baths") or 0
            document["baths"] = float(document["full_baths"]) + (0.5 * float(half))

        # Normalize photo links as a list.
        photo_links = document.get("photo_links")
        if isinstance(photo_links, str):
            document["photo_links"] = [p.strip() for p in photo_links.split(",") if p.strip()]
        elif isinstance(photo_links, list):
            document["photo_links"] = [p for p in photo_links if isinstance(p, str) and p.strip()]
        else:
            first_photo = document.get("first_photo_url")
            document["photo_links"] = [first_photo] if isinstance(first_photo, str) and first_photo.strip() else []

        # Keep first_photo_url canonical and always aligned with the first photo link.
        if document.get("first_photo_url") in (None, "", []):
            links = document.get("photo_links")
            if isinstance(links, list) and links:
                document["first_photo_url"] = links[0]

        # Basic location normalization.
        if isinstance(document.get("state"), str):
            document["state"] = document["state"].strip().upper() or None
        if isinstance(document.get("postal_code"), str):
            document["postal_code"] = document["postal_code"].strip() or None

        document["canonical_schema_version"] = self.CANONICAL_SCHEMA_VERSION
        return document

    @staticmethod
    def _first_present(document, *keys):
        for key in keys:
            value = document.get(key)
            if value not in (None, "", []):
                return value
        return None

    @staticmethod
    def _to_int(value):
        if value in (None, "", []):
            return None
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(value))
            if cleaned in ("", "-", ".", "-."):
                return None
            return int(float(cleaned))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value):
        if value in (None, "", []):
            return None
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(value))
            if cleaned in ("", "-", ".", "-."):
                return None
            return float(cleaned)
        except (TypeError, ValueError):
            return None

    def _normalize_status(self, value):
        if value in (None, "", []):
            return None
        if isinstance(value, dict):
            return self._first_present(value, "DISPLAYVALUE", "displayValue", "status", "value")
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if stripped.startswith("{") and "DISPLAYVALUE" in stripped:
                try:
                    parsed = json.loads(stripped.replace("'", '"'))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return stripped
                return self._normalize_status(parsed)
            return stripped
        return str(value)

    def _enrich_zillow_from_raw(self, document):
        source = str(document.get("source") or "").strip().lower()
        if source != "zillow":
            return
        raw = document.get("raw_listing")
        if not isinstance(raw, dict):
            return
        hdp_data = raw.get("hdpData") or {}
        if not isinstance(hdp_data, dict):
            hdp_data = {}
        home_info = hdp_data.get("homeInfo") or {}
        if not isinstance(home_info, dict):
            home_info = {}

        def coalesce(*values):
            for value in values:
                if value not in (None, "", []):
                    return value
            return None

        if document.get("living_area_sqft") in (None, "", []):
            document["living_area_sqft"] = self._to_int(
                coalesce(home_info.get("livingArea"), raw.get("livingArea"), raw.get("area"))
            )
        if document.get("tax_assessed_value") in (None, "", []):
            document["tax_assessed_value"] = self._to_int(
                coalesce(home_info.get("taxAssessedValue"), raw.get("taxAssessedValue"))
            )
        if document.get("days_on_zillow") in (None, "", []):
            document["days_on_zillow"] = self._to_int(
                coalesce(home_info.get("daysOnZillow"), raw.get("daysOnZillow"))
            )
        if document.get("is_preforeclosure_auction") in (None, "", []):
            document["is_preforeclosure_auction"] = coalesce(
                home_info.get("isPreforeclosureAuction"),
                raw.get("isPreforeclosureAuction"),
            )
        if document.get("lot_area_value") in (None, "", []):
            document["lot_area_value"] = self._to_float(
                coalesce(home_info.get("lotAreaValue"), raw.get("lotAreaValue"))
            )
        if document.get("lot_area_unit") in (None, "", []):
            document["lot_area_unit"] = coalesce(home_info.get("lotAreaUnit"), raw.get("lotAreaUnit"))

        photo_links = document.get("photo_links")
        has_photo_links = isinstance(photo_links, list) and len(photo_links) > 0
        if not has_photo_links:
            links = self._extract_zillow_photo_links(raw, home_info)
            if links:
                document["photo_links"] = links
                document["photos_count"] = len(links)
                document["first_photo_url"] = links[0]

    @staticmethod
    def _extract_zillow_photo_links(raw, home_info):
        links = []
        seen = set()

        def add(url):
            if not isinstance(url, str):
                return
            value = url.strip()
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

        carousel = raw.get("carouselPhotosComposable")
        if not isinstance(carousel, dict):
            carousel = home_info.get("carouselPhotosComposable") if isinstance(home_info, dict) else None
        if isinstance(carousel, dict):
            base_url = carousel.get("baseUrl")
            photo_data = carousel.get("photoData") or []
            if isinstance(base_url, str) and base_url.strip() and isinstance(photo_data, list):
                for photo in photo_data:
                    if not isinstance(photo, dict):
                        continue
                    key = photo.get("photoKey")
                    if not isinstance(key, str) or not key.strip():
                        continue
                    add(base_url.replace("{photoKey}", key.strip()))

        for key in ("imgSrc", "hdpImageLink", "img", "thumb"):
            add(raw.get(key))
            if isinstance(home_info, dict):
                add(home_info.get(key))
        for key in ("imgSrcs", "photos"):
            values = raw.get(key)
            if values in (None, "") and isinstance(home_info, dict):
                values = home_info.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        add(value.get("url"))
                    else:
                        add(value)

        return links


class PropertyValidationPipeline:
    """Attach lightweight schema validation signals without breaking crawls."""

    @staticmethod
    def process_item(item, spider):
        if item.get("__artifact_type__"):
            return item

        document = dict(item)
        errors = []

        has_identity = any(
            document.get(key) not in (None, "", [])
            for key in ("source_listing_id", "mls_id", "detail_url")
        )
        if not has_identity:
            errors.append("missing_identity")

        lat = document.get("latitude")
        lon = document.get("longitude")
        if lat is not None and not (-90 <= lat <= 90):
            errors.append("invalid_latitude")
        if lon is not None and not (-180 <= lon <= 180):
            errors.append("invalid_longitude")

        year_built = document.get("year_built")
        if isinstance(year_built, int):
            current_year = datetime.now(timezone.utc).year
            if year_built < 1700 or year_built > current_year + 2:
                errors.append("invalid_year_built")

        if errors:
            document["validation_errors"] = errors
            document["parse_status"] = "partial"
        else:
            document["parse_status"] = document.get("parse_status") or "ok"
            document.setdefault("validation_errors", [])

        return document


class PropertyItemEnvelopePipeline:
    """Wrap normalized dict items into PropertyItem and preserve extras in source_fields."""

    @staticmethod
    def process_item(item, spider):
        if item.get("__artifact_type__"):
            return item

        document = dict(item)
        allowed = set(PropertyItem.fields.keys())
        envelope = {}

        existing_source_fields = document.get("source_fields")
        source_fields = dict(existing_source_fields) if isinstance(existing_source_fields, dict) else {}

        for key, value in document.items():
            if key == "source_fields":
                continue
            if key in allowed:
                envelope[key] = value
            else:
                source_fields[key] = value

        if source_fields:
            envelope["source_fields"] = source_fields

        return PropertyItem(envelope)


class MongoPipeline:
    """Write scraped items to MongoDB with duplicate-safe upsert behavior."""

    DEFAULT_ID_FIELDS = (
        "id",
        "zpid",
        "property_id",
        "listing_id",
        "mls_id",
        "mls_number",
        "source_mls",
        "source_listing_id"
    )

    def __init__(
        self,
        mongo_uri,
        mongo_database,
        collection_prefix,
        max_pool_size,
        min_pool_size,
        compressors,
        zlib_compression_level,
    ):
        self.mongo_uri = mongo_uri
        self.mongo_database = mongo_database
        self.collection_prefix = collection_prefix
        self.max_pool_size = max_pool_size
        self.min_pool_size = min_pool_size
        self.compressors = compressors
        self.zlib_compression_level = zlib_compression_level
        self.client = None
        self.db = None
        self.collections = {}

    @classmethod
    def from_crawler(cls, crawler):
        mongo_uri = get_env("MONGO_URI", "MONGODB_URI")
        mongo_database = get_env("MONGO_DATABASE", "MONGODB_DATABASE", default="realestate")
        collection_prefix = get_env("MONGO_COLLECTION_PREFIX", default="")

        if not mongo_uri:
            raise RuntimeError(
                "MongoDB URI not found. Set MONGO_URI or MONGODB_URI in env or settings/.env."
            )

        max_pool_size = int(get_env("DB_MAX_POOL_SIZE", default="100"))
        min_pool_size = int(get_env("DB_MIN_POOL_SIZE", default="0"))
        compressors = get_env("DB_COMPRESSORS", default="zstd")
        zlib_compression_level = int(get_env("DB_ZLIB_COMPRESSION_LEVEL", default="9"))

        return cls(
            mongo_uri=mongo_uri,
            mongo_database=mongo_database,
            collection_prefix=collection_prefix,
            max_pool_size=max_pool_size,
            min_pool_size=min_pool_size,
            compressors=compressors,
            zlib_compression_level=zlib_compression_level,
        )

    def open_spider(self, spider):
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi

        self.client = MongoClient(
            self.mongo_uri,
            server_api=ServerApi("1"),
            maxPoolSize=self.max_pool_size,
            minPoolSize=self.min_pool_size,
            compressors=self.compressors,
            zlibCompressionLevel=self.zlib_compression_level,
        )
        self.db = self.client[self.mongo_database]

    def close_spider(self, spider):
        if self.client:
            self.client.close()
            self.client = None

    def process_item(self, item, spider):
        document = dict(item)
        now = datetime.now(timezone.utc)

        collection = self._collection_for_spider(spider.name)
        identity_filter = self._identity_filter(document)

        if identity_filter:
            collection.update_one(
                identity_filter,
                {
                    "$set": {
                        **document,
                        "spider": spider.name,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
        else:
            collection.insert_one(
                {
                    **document,
                    "spider": spider.name,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        return item

    def _collection_for_spider(self, spider_name):
        name = f"{self.collection_prefix}{spider_name}" if self.collection_prefix else spider_name
        if name not in self.collections:
            self.collections[name] = self.db[name]
        return self.collections[name]

    def _identity_filter(self, document):
        for field in self.DEFAULT_ID_FIELDS:
            value = document.get(field)
            if value not in (None, "", []):
                return {field: value}
        return None
