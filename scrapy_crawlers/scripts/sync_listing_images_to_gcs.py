#!/usr/bin/env python3
"""
Download listing images from Mongo photo_links and upload to GCS with dedupe.

Dedupe strategy:
1) Exact-byte dedupe by SHA-256 content hash (primary).
2) Store one canonical object per hash under:
   {prefix}/sha256/{first2}/{sha256}.{ext}
3) Reuse existing hash record instead of re-uploading duplicates.

Optional:
- Create bucket if missing.
- Limit number of processed listings for smoke testing.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _bootstrap_paths() -> None:
    here = Path(__file__).resolve()
    scrapy_root = here.parents[1]  # .../scrapy_crawlers
    if str(scrapy_root) not in sys.path:
        sys.path.insert(0, str(scrapy_root))


_bootstrap_paths()

from spiders.env_config import get_env  # noqa: E402


DEFAULT_COLLECTIONS = "njmls,remax,weichert,redfin,realtor,bhgre,gsmls,zillow"
IMAGE_ASSET_COLLECTION = "image_assets"
DEFAULT_RESUME_DB = "scrapy_crawlers/scripts/image_sync_resume.sqlite3"
LISTING_PROJECTION = {
    "_id": 1,
    "source": 1,
    "mls_id": 1,
    "source_listing_id": 1,
    "detail_url": 1,
    "photo_links": 1,
    "first_photo_url": 1,
    "primary_photo_url": 1,
    "source_fields.first_photo_url": 1,
    "source_fields.primary_photo_url": 1,
}


@dataclass(frozen=True)
class FetchedImage:
    index: int
    url: str
    content: bytes
    sha256: str
    ext: str


class ProgressTracker:
    def __init__(self, total_docs_estimate: int, every_n_docs: int):
        self.total_docs_estimate = total_docs_estimate
        self.every_n_docs = max(1, every_n_docs)
        self.docs_processed = 0
        self.started = time.time()
        self.last_report = self.started

    def stage(self, stage_name: str, message: str) -> None:
        print(f"[stage:{stage_name}] {message}")

    def tick_doc(self) -> None:
        self.docs_processed += 1
        now = time.time()
        should_report = (
            self.docs_processed % self.every_n_docs == 0 or (now - self.last_report) >= 30
        )
        if not should_report:
            return

        elapsed = max(now - self.started, 0.001)
        rate = self.docs_processed / elapsed
        if self.total_docs_estimate > 0:
            remaining = max(self.total_docs_estimate - self.docs_processed, 0)
            eta_sec = int(remaining / rate) if rate > 0 else -1
            pct = (100.0 * self.docs_processed) / self.total_docs_estimate
            print(
                "[progress]",
                f"docs={self.docs_processed}/{self.total_docs_estimate}",
                f"pct={pct:.1f}",
                f"rate_docs_per_sec={rate:.2f}",
                f"eta_sec={eta_sec}",
            )
        else:
            print(
                "[progress]",
                f"docs={self.docs_processed}",
                f"rate_docs_per_sec={rate:.2f}",
            )
        self.last_report = now


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResumeQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_name TEXT NOT NULL,
                doc_id_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                enqueued_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(collection_name, doc_id_json)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listing_queue_collection_status "
            "ON listing_queue(collection_name, status)"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def clear(self, collections: List[str]) -> None:
        placeholders = ",".join("?" for _ in collections)
        self.conn.execute(
            f"DELETE FROM listing_queue WHERE collection_name IN ({placeholders})",
            collections,
        )
        self.conn.commit()

    def enqueue(self, collection_name: str, doc_id_json: str) -> bool:
        now = utc_now_iso()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO listing_queue (
                collection_name, doc_id_json, status, attempts, enqueued_at, updated_at
            ) VALUES (?, ?, 'pending', 0, ?, ?)
            """,
            (collection_name, doc_id_json, now, now),
        )
        return bool(cur.rowcount)

    def finalize_enqueue(self) -> None:
        self.conn.commit()

    def reset_in_progress(self) -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            """
            UPDATE listing_queue
            SET status='pending', updated_at=?
            WHERE status='in_progress'
            """,
            (now,),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    def reset_failed(self, collections: List[str]) -> int:
        if not collections:
            return 0
        placeholders = ",".join("?" for _ in collections)
        now = utc_now_iso()
        cur = self.conn.execute(
            f"""
            UPDATE listing_queue
            SET status='pending', last_error=NULL, updated_at=?
            WHERE status='failed' AND collection_name IN ({placeholders})
            """,
            (now, *collections),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    def next_pending(self, collection_name: str, limit: int) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT id, doc_id_json, attempts
            FROM listing_queue
            WHERE collection_name = ? AND status = 'pending'
            ORDER BY id ASC
            LIMIT ?
            """,
            (collection_name, max(1, limit)),
        )
        return cur.fetchall()

    def mark_in_progress(self, row_id: int) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE listing_queue
            SET status='in_progress', attempts=attempts+1, updated_at=?
            WHERE id = ?
            """,
            (now, row_id),
        )
        self.conn.commit()

    def mark_done(self, row_id: int) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE listing_queue
            SET status='done', last_error=NULL, updated_at=?
            WHERE id = ?
            """,
            (now, row_id),
        )
        self.conn.commit()

    def mark_failed(self, row_id: int, error: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            UPDATE listing_queue
            SET status='failed', last_error=?, updated_at=?
            WHERE id = ?
            """,
            (error[:1200], now, row_id),
        )
        self.conn.commit()

    def count_by_status(self, collection_name: str, status: str) -> int:
        cur = self.conn.execute(
            """
            SELECT COUNT(1) FROM listing_queue
            WHERE collection_name = ? AND status = ?
            """,
            (collection_name, status),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def count_status_for_collections(self, collections: List[str], status: str) -> int:
        if not collections:
            return 0
        placeholders = ",".join("?" for _ in collections)
        cur = self.conn.execute(
            f"""
            SELECT COUNT(1) FROM listing_queue
            WHERE status = ? AND collection_name IN ({placeholders})
            """,
            (status, *collections),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync listing photos from MongoDB to GCS with SHA-256 dedupe."
    )
    parser.add_argument(
        "--collections",
        default=get_env("IMAGE_SYNC_COLLECTIONS", default=DEFAULT_COLLECTIONS),
        help=f"Comma-separated Mongo collections (default: {DEFAULT_COLLECTIONS})",
    )
    parser.add_argument(
        "--limit-listings",
        type=int,
        default=0,
        help="Max listings per collection to process (0 = no limit).",
    )
    parser.add_argument(
        "--max-images-per-listing",
        type=int,
        default=int(get_env("IMAGE_SYNC_MAX_IMAGES_PER_LISTING", default="30")),
        help="Max photo links to process for each listing.",
    )
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="Create the configured bucket if it does not exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not upload/update Mongo; only print actions.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(get_env("IMAGE_SYNC_WORKERS", default="8")),
        help="Concurrent worker threads for image fetch + fingerprint.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=int(get_env("IMAGE_SYNC_CHUNK_SIZE", default="100")),
        help="Process each listing's photos in worker chunks of this size.",
    )
    parser.add_argument(
        "--progress-every-docs",
        type=int,
        default=int(get_env("IMAGE_SYNC_PROGRESS_EVERY_DOCS", default="50")),
        help="Print progress every N processed listings.",
    )
    parser.add_argument(
        "--resume-db",
        default=get_env("IMAGE_SYNC_RESUME_DB", default=DEFAULT_RESUME_DB),
        help=f"SQLite resume queue path (default: {DEFAULT_RESUME_DB}).",
    )
    parser.add_argument(
        "--queue-batch-size",
        type=int,
        default=int(get_env("IMAGE_SYNC_QUEUE_BATCH_SIZE", default="200")),
        help="How many pending listings to dequeue per batch.",
    )
    parser.add_argument(
        "--reset-queue",
        action="store_true",
        help="Clear queued state for target collections and rebuild from Mongo.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Move failed queue entries back to pending before processing.",
    )
    return parser.parse_args()


def require_env(name: str, fallback: Optional[str] = None) -> str:
    value = get_env(name, default=fallback) if fallback is not None else get_env(name)
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def resolve_google_credentials_path() -> Optional[Path]:
    raw = get_env("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # Resolve relative to repository root and settings dir.
        cwd_candidate = (Path.cwd() / path).resolve()
        settings_candidate = (Path.cwd() / "scrapy_crawlers" / "settings" / path).resolve()
        if cwd_candidate.exists():
            path = cwd_candidate
        elif settings_candidate.exists():
            path = settings_candidate
    if not path.exists():
        return None
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return path


def resolve_config() -> Dict[str, str]:
    credentials_path = resolve_google_credentials_path()
    project_id = require_env("GCP_PROJECT_ID")

    cfg = {
        "mongo_uri": require_env("MONGO_URI", fallback=get_env("MONGODB_URI")),
        "mongo_db": get_env("MONGO_DATABASE", "MONGODB_DATABASE", default="realestate"),
        "gcp_project_id": project_id,
        "gcs_bucket": require_env("GCS_IMAGE_BUCKET"),
        "gcs_prefix": get_env("GCS_IMAGE_PREFIX", default="property-images"),
        "gcs_bucket_location": get_env("GCS_BUCKET_LOCATION", default="US"),
        "user_agent": get_env(
            "IMAGE_SYNC_USER_AGENT",
            default=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
            ),
        ),
    }
    if credentials_path:
        cfg["google_credentials_path"] = str(credentials_path)
    return cfg


def ensure_bucket(client, bucket_name: str, project_id: str, location: str, create_if_missing: bool):
    bucket = client.bucket(bucket_name)
    if bucket.exists():
        return bucket, False
    if not create_if_missing:
        raise RuntimeError(
            f"GCS bucket '{bucket_name}' not found. Re-run with --create-bucket or create it manually."
        )
    bucket = client.create_bucket(bucket, project=project_id, location=location)
    return bucket, True


def build_gcs_client(storage_module, cfg: Dict[str, str]):
    credentials_path = cfg.get("google_credentials_path")
    project_id = cfg["gcp_project_id"]

    if credentials_path:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(credentials_path)
        return storage_module.Client(project=project_id, credentials=creds)

    from google.auth.exceptions import DefaultCredentialsError

    try:
        return storage_module.Client(project=project_id)
    except DefaultCredentialsError as exc:
        raise RuntimeError(
            "Google Cloud credentials were not found. Set GOOGLE_APPLICATION_CREDENTIALS to a "
            "service account key JSON, or provide a valid relative path that exists under "
            "the current working directory or scrapy_crawlers/settings."
        ) from exc


def detect_extension(content: bytes, source_url: str) -> str:
    head = content[:16]
    kind = None
    if head.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        kind = "gif"
    elif len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        kind = "webp"

    if kind == "jpeg":
        return "jpg"
    if kind in {"png", "webp", "gif"}:
        return kind
    lower = (source_url or "").lower()
    for ext in ("jpg", "jpeg", "png", "webp", "gif"):
        if f".{ext}" in lower:
            return "jpg" if ext == "jpeg" else ext
    return "bin"


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def object_name_for_hash(prefix: str, sha256_hex: str, ext: str) -> str:
    safe_prefix = (prefix or "").strip("/ ")
    root = f"{safe_prefix}/sha256" if safe_prefix else "sha256"
    return f"{root}/{sha256_hex[:2]}/{sha256_hex}.{ext}"


def fetch_image_bytes(url: str, user_agent: str, timeout_sec: int = 25) -> Optional[bytes]:
    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.google.com/",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read()
            if not body:
                return None
            if "text/html" in content_type and b"<html" in body[:512].lower():
                return None
            return body
    except (HTTPError, URLError, TimeoutError):
        return None


def iter_target_doc_ids(collection, limit: int):
    query = build_target_docs_query()
    cursor = collection.find(query, {"_id": 1})
    if limit and limit > 0:
        cursor = cursor.limit(limit)
    return cursor


def build_target_docs_query() -> Dict[str, Any]:
    return {
        "$or": [
            {"photo_links.0": {"$exists": True}},
            {"first_photo_url": {"$exists": True, "$type": "string", "$ne": ""}},
            # Legacy fallback for older rows before canonical aliasing.
            {"primary_photo_url": {"$exists": True, "$type": "string", "$ne": ""}},
            {"source_fields.first_photo_url": {"$exists": True, "$type": "string", "$ne": ""}},
            # Legacy fallback for older rows before canonical aliasing.
            {"source_fields.primary_photo_url": {"$exists": True, "$type": "string", "$ne": ""}},
        ]
    }


def collect_photo_urls(doc: Dict, max_images: int) -> List[str]:
    urls: List[str] = []
    seen = set()

    def is_blocked_source_url(value: str) -> bool:
        lowered = value.lower()
        return "njmls.com/assets/" in lowered

    def add(value):
        if not isinstance(value, str):
            return
        v = value.strip()
        if not v or v in seen:
            return
        if is_blocked_source_url(v):
            return
        seen.add(v)
        urls.append(v)

    # Canonical first-photo field first, then full gallery links.
    add(doc.get("first_photo_url"))
    source_fields = doc.get("source_fields") or {}
    if isinstance(source_fields, dict):
        add(source_fields.get("first_photo_url"))
        # Legacy fallback for older rows where primary_photo_url was not canonicalized yet.
        add(source_fields.get("primary_photo_url"))
    # Legacy fallback for older rows where primary_photo_url was still top-level.
    add(doc.get("primary_photo_url"))

    photo_links = doc.get("photo_links") or []
    if isinstance(photo_links, list):
        for value in photo_links:
            add(value)
    elif isinstance(photo_links, str):
        for value in photo_links.split(","):
            add(value)

    if max_images > 0:
        return urls[:max_images]
    return urls


def chunked(items: List[Tuple[int, str]], size: int) -> Iterator[List[Tuple[int, str]]]:
    if size <= 0:
        size = len(items) or 1
    for start in range(0, len(items), size):
        yield items[start : start + size]


def fetch_and_fingerprint(index: int, url: str, user_agent: str) -> Optional[FetchedImage]:
    content = fetch_image_bytes(url=url, user_agent=user_agent)
    if not content:
        return None
    sha = content_sha256(content)
    ext = detect_extension(content, source_url=url)
    return FetchedImage(index=index, url=url, content=content, sha256=sha, ext=ext)


def fetch_listing_images(
    photo_urls: List[str], user_agent: str, workers: int, chunk_size: int
) -> Tuple[List[FetchedImage], int]:
    indexed_urls = list(enumerate(photo_urls))
    if not indexed_urls:
        return [], 0

    effective_workers = max(1, workers)
    results: List[FetchedImage] = []
    failed_fetches = 0

    for url_chunk in chunked(indexed_urls, chunk_size):
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = [
                pool.submit(fetch_and_fingerprint, idx, url, user_agent)
                for idx, url in url_chunk
            ]
            for future in as_completed(futures):
                item = future.result()
                if item is None:
                    failed_fetches += 1
                else:
                    results.append(item)

    results.sort(key=lambda item: item.index)
    return results, failed_fetches


def main() -> int:
    args = parse_args()
    cfg = resolve_config()
    workers = max(1, int(args.workers))
    chunk_size = max(1, int(args.chunk_size))
    progress_every_docs = max(1, int(args.progress_every_docs))
    queue_batch_size = max(1, int(args.queue_batch_size))
    resume_db_path = Path(args.resume_db).expanduser()
    if not resume_db_path.is_absolute():
        resume_db_path = (Path.cwd() / resume_db_path).resolve()

    try:
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
        from bson import json_util
    except ModuleNotFoundError as exc:
        raise RuntimeError("pymongo missing. Install dependencies in your venv.") from exc

    try:
        from google.cloud import storage
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "google-cloud-storage missing. Install with: .venv/bin/pip install google-cloud-storage"
        ) from exc

    client = MongoClient(cfg["mongo_uri"], server_api=ServerApi("1"))
    db = client[cfg["mongo_db"]]
    images_coll = db[IMAGE_ASSET_COLLECTION]
    images_coll.create_index("sha256", unique=True)
    images_coll.create_index("gcs_uri", unique=True, sparse=True)

    gcs_client = build_gcs_client(storage, cfg)
    bucket, created = ensure_bucket(
        gcs_client,
        bucket_name=cfg["gcs_bucket"],
        project_id=cfg["gcp_project_id"],
        location=cfg["gcs_bucket_location"],
        create_if_missing=args.create_bucket,
    )
    if created:
        print(f"Created bucket: {bucket.name}")

    collections = [c.strip() for c in str(args.collections or "").split(",") if c.strip()]
    if not collections:
        print("No collections provided.")
        return 0

    progress = ProgressTracker(total_docs_estimate=0, every_n_docs=progress_every_docs)
    progress.stage("init", f"resume_db={resume_db_path} workers={workers} chunk_size={chunk_size}")
    resume_queue = ResumeQueue(resume_db_path)
    if args.reset_queue:
        progress.stage("queue-reset", "clearing queued state for target collections")
        resume_queue.clear(collections)
    recovered = resume_queue.reset_in_progress()
    if recovered:
        progress.stage("queue-recover", f"moved_in_progress_to_pending={recovered}")
    if args.retry_failed:
        retried = resume_queue.reset_failed(collections)
        progress.stage("queue-retry-failed", f"failed_to_pending={retried}")

    progress.stage("queue-build", "scanning Mongo and enqueueing listings")
    queued_new = 0
    for coll_name in collections:
        collection = db[coll_name]
        for doc in iter_target_doc_ids(collection, limit=args.limit_listings):
            doc_id_json = json_util.dumps(doc["_id"], sort_keys=True)
            if resume_queue.enqueue(coll_name, doc_id_json):
                queued_new += 1
        resume_queue.finalize_enqueue()
    total_docs_estimate = resume_queue.count_status_for_collections(collections, "pending")
    progress.total_docs_estimate = total_docs_estimate
    progress.stage(
        "queue-ready",
        (
            f"pending={total_docs_estimate} queued_new={queued_new} "
            f"dry_run={args.dry_run} queue_batch_size={queue_batch_size}"
        ),
    )

    started = time.time()
    total_docs = 0
    total_uploaded = 0
    total_reused = 0
    total_failed = 0
    total_docs_no_photo_inputs = 0
    total_docs_all_fetches_failed = 0
    total_docs_no_images_written = 0
    total_docs_synced = 0

    try:
        for coll_name in collections:
            collection = db[coll_name]
            pending_at_start = resume_queue.count_by_status(coll_name, "pending")
            progress.stage(
                "collection-start",
                f"name={coll_name} pending={pending_at_start}",
            )
            doc_count = 0
            while True:
                rows = resume_queue.next_pending(coll_name, limit=queue_batch_size)
                if not rows:
                    break
                for row in rows:
                    row_id = int(row["id"])
                    doc_id_json = str(row["doc_id_json"])
                    resume_queue.mark_in_progress(row_id)
                    try:
                        listing_id = json_util.loads(doc_id_json)
                        doc = collection.find_one({"_id": listing_id}, LISTING_PROJECTION)
                        if not doc:
                            resume_queue.mark_done(row_id)
                            continue

                        photos = collect_photo_urls(doc, max_images=args.max_images_per_listing)
                        if not photos:
                            total_docs_no_photo_inputs += 1
                            resume_queue.mark_done(row_id)
                            continue

                        fetched_images, failed_fetches = fetch_listing_images(
                            photo_urls=photos,
                            user_agent=cfg["user_agent"],
                            workers=workers,
                            chunk_size=chunk_size,
                        )
                        total_failed += failed_fetches
                        if not fetched_images:
                            total_docs_all_fetches_failed += 1
                            resume_queue.mark_failed(row_id, "all_photo_fetches_failed")
                            continue

                        images_out: List[Dict] = []
                        seen_hashes = set()
                        for item in fetched_images:
                            sha = item.sha256
                            if sha in seen_hashes:
                                continue
                            seen_hashes.add(sha)

                            asset_doc = images_coll.find_one(
                                {"sha256": sha},
                                {"gcs_uri": 1, "object_name": 1},
                            )
                            if asset_doc and asset_doc.get("gcs_uri"):
                                gcs_uri = asset_doc["gcs_uri"]
                                total_reused += 1
                            else:
                                object_name = object_name_for_hash(cfg["gcs_prefix"], sha, item.ext)
                                gcs_uri = f"gs://{bucket.name}/{object_name}"
                                if not args.dry_run:
                                    try:
                                        blob = bucket.blob(object_name)
                                        if not blob.exists():
                                            blob.upload_from_string(
                                                item.content,
                                                content_type=f"image/{item.ext}",
                                            )
                                        images_coll.update_one(
                                            {"sha256": sha},
                                            {
                                                "$setOnInsert": {
                                                    "sha256": sha,
                                                    "gcs_uri": gcs_uri,
                                                    "object_name": object_name,
                                                    "size_bytes": len(item.content),
                                                    "created_at": datetime.now(timezone.utc),
                                                }
                                            },
                                            upsert=True,
                                        )
                                    except Exception:
                                        total_failed += 1
                                        continue
                                total_uploaded += 1

                            images_out.append(
                                {
                                    "index": item.index,
                                    "source_url": item.url,
                                    "sha256": sha,
                                    "gcs_uri": gcs_uri,
                                }
                            )

                        if not images_out:
                            total_docs_no_images_written += 1
                            resume_queue.mark_failed(row_id, "no_images_written_after_processing")
                            continue

                        if not args.dry_run and images_out:
                            collection.update_one(
                                {"_id": doc["_id"]},
                                {
                                    "$set": {
                                        "gcs_images": images_out,
                                        "gcs_images_count": len(images_out),
                                        "gcs_images_synced_at": datetime.now(timezone.utc),
                                    }
                                },
                            )
                        total_docs_synced += 1
                        resume_queue.mark_done(row_id)
                    except Exception as exc:
                        total_failed += 1
                        resume_queue.mark_failed(row_id, str(exc))
                    finally:
                        doc_count += 1
                        total_docs += 1
                        progress.tick_doc()
            pending_left = resume_queue.count_by_status(coll_name, "pending")
            failed_count = resume_queue.count_by_status(coll_name, "failed")
            progress.stage(
                "collection-done",
                f"name={coll_name} processed_docs={doc_count} pending_left={pending_left} failed={failed_count}",
            )
    finally:
        resume_queue.close()

    elapsed = time.time() - started
    print(
        "sync complete",
        f"collections={len(collections)}",
        f"docs={total_docs}",
        f"docs_synced={total_docs_synced}",
        f"uploaded_new_objects={total_uploaded}",
        f"reused={total_reused}",
        f"failed_fetches={total_failed}",
        f"docs_no_photo_inputs={total_docs_no_photo_inputs}",
        f"docs_all_fetches_failed={total_docs_all_fetches_failed}",
        f"docs_no_images_written={total_docs_no_images_written}",
        f"elapsed_sec={elapsed:.1f}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
