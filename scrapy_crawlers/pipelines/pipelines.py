import json
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from pymongo.server_api import ServerApi
from scrapy.exceptions import DropItem

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
