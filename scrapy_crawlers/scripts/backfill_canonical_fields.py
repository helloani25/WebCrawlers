#!/usr/bin/env python3
"""
Backfill canonical property fields for existing MongoDB collections.

Uses the same normalization + validation logic as Scrapy pipelines so
historical documents become query-compatible with newly crawled records.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Dict, List


def _bootstrap_import_paths() -> None:
    here = Path(__file__).resolve()
    scrapy_root = here.parents[1]  # .../scrapy_crawlers
    if str(scrapy_root) not in sys.path:
        sys.path.insert(0, str(scrapy_root))


_bootstrap_import_paths()

from pipelines.pipelines import PropertyNormalizationPipeline, PropertyValidationPipeline  # noqa: E402
from spiders.env_config import get_env  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill canonical schema fields in Mongo collections."
    )
    parser.add_argument(
        "--collections",
        default="",
        help="Comma-separated collection names. Default: all non-system collections.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for cursor and bulk updates (default: 500).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max documents per collection (0 = no limit).",
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="Process all docs, not just missing/outdated canonical schema.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute diffs and print stats without writing to MongoDB.",
    )
    parser.add_argument(
        "--database",
        default="",
        help="Override DB name (default from MONGO_DATABASE/MONGODB_DATABASE).",
    )
    parser.add_argument(
        "--uri",
        default="",
        help="Override Mongo URI (default from MONGO_URI/MONGODB_URI).",
    )
    parser.add_argument(
        "--cursor-no-timeout",
        action="store_true",
        help=(
            "Request Mongo noTimeout cursors. Disabled by default for Atlas "
            "tiers that disallow this option."
        ),
    )
    return parser.parse_args()


class _SpiderStub:
    def __init__(self, name: str):
        self.name = name


def resolve_mongo_settings(args: argparse.Namespace) -> tuple[str, str]:
    mongo_uri = args.uri or get_env("MONGO_URI", "MONGODB_URI")
    mongo_db = args.database or get_env(
        "MONGO_DATABASE", "MONGODB_DATABASE", default="realestate"
    )
    if not mongo_uri:
        raise RuntimeError("MongoDB URI missing. Set MONGO_URI or pass --uri.")
    return mongo_uri, mongo_db


def list_target_collections(db, explicit: str) -> List[str]:
    if explicit.strip():
        return [c.strip() for c in explicit.split(",") if c.strip()]
    names = db.list_collection_names()
    return [n for n in names if not n.startswith("system.")]


def base_query(force_all: bool) -> Dict:
    if force_all:
        return {}
    return {
        "$or": [
            {"canonical_schema_version": {"$exists": False}},
            {"canonical_schema_version": {"$lt": PropertyNormalizationPipeline.CANONICAL_SCHEMA_VERSION}},
        ]
    }


def diff_fields(old_doc: Dict, new_doc: Dict) -> Dict:
    updates = {}
    for key, new_value in new_doc.items():
        if key == "_id":
            continue
        if old_doc.get(key) != new_value:
            updates[key] = new_value
    return updates


def process_collection(
    collection,
    query: Dict,
    batch_size: int,
    limit: int,
    dry_run: bool,
    cursor_no_timeout: bool,
) -> Dict[str, int]:
    from pymongo import UpdateOne

    norm = PropertyNormalizationPipeline()
    validate = PropertyValidationPipeline()
    spider = _SpiderStub(collection.name)

    cursor = collection.find(query, no_cursor_timeout=cursor_no_timeout).batch_size(batch_size)
    if limit and limit > 0:
        cursor = cursor.limit(limit)

    scanned = 0
    changed = 0
    written = 0
    skipped = 0
    ops: List[UpdateOne] = []

    try:
        for doc in cursor:
            scanned += 1
            normalized = norm.process_item(doc, spider)
            normalized = validate.process_item(normalized, spider)
            normalized["canonical_backfilled_at"] = datetime.now(timezone.utc)

            updates = diff_fields(doc, normalized)
            if not updates:
                skipped += 1
                continue

            changed += 1
            if dry_run:
                continue

            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": updates}))
            if len(ops) >= batch_size:
                result = collection.bulk_write(ops, ordered=False)
                written += int(result.modified_count or 0)
                ops = []
    finally:
        cursor.close()

    if ops and not dry_run:
        result = collection.bulk_write(ops, ordered=False)
        written += int(result.modified_count or 0)

    return {
        "scanned": scanned,
        "changed": changed,
        "written": written,
        "skipped": skipped,
    }


def main() -> int:
    args = parse_args()

    try:
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pymongo is required to run backfill. Activate your project venv first."
        ) from exc

    mongo_uri, mongo_db = resolve_mongo_settings(args)
    query = base_query(force_all=args.force_all)

    client = MongoClient(mongo_uri, server_api=ServerApi("1"))
    db = client[mongo_db]
    collections = list_target_collections(db, args.collections)

    if not collections:
        print("No collections to process.")
        return 0

    print(
        f"Backfill start db={mongo_db} dry_run={args.dry_run} "
        f"batch_size={args.batch_size} limit={args.limit or 'none'} "
        f"force_all={args.force_all} "
        f"target_schema_version={PropertyNormalizationPipeline.CANONICAL_SCHEMA_VERSION}"
    )
    print(f"Collections: {', '.join(collections)}")

    total_scanned = 0
    total_changed = 0
    total_written = 0
    total_skipped = 0

    for name in collections:
        coll = db[name]
        stats = process_collection(
            collection=coll,
            query=query,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
            cursor_no_timeout=args.cursor_no_timeout,
        )
        total_scanned += stats["scanned"]
        total_changed += stats["changed"]
        total_written += stats["written"]
        total_skipped += stats["skipped"]
        print(
            f"[{name}] scanned={stats['scanned']} changed={stats['changed']} "
            f"written={stats['written']} skipped={stats['skipped']}"
        )

    print(
        "Backfill complete "
        f"scanned={total_scanned} changed={total_changed} "
        f"written={total_written} skipped={total_skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
