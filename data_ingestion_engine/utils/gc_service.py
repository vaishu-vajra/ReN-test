# utils/gc_service.py
# Full, hardened GCS helper module with explicit OAuth scopes and per-process clients.
# - Eliminates flaky 403 "Provided scope(s) are not authorized" by pinning scopes.
# - Avoids global storage.Client() created at import time (bad with Celery forking).
# - Adds timeouts to GCS operations.
# - Keeps MongoDB helpers as in your original file.

from datetime import datetime
import math
from bson import ObjectId
from urllib.parse import urlparse, unquote
import os
import time
import logging
from urllib.parse import unquote, urlparse
from typing import Tuple, Optional, List
import certifi
from dotenv import load_dotenv
from pymongo import MongoClient
# Google Cloud
from google.cloud import storage
from google.oauth2 import service_account
import google.auth
from google.api_core.exceptions import Forbidden, NotFound
import requests

from data_ingestion_engine.utils.download_file import download_file

# ──────────────────────────────────────────────────────────────────────────────
# Load environment variables
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

# GCS CONFIG
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
if not GCS_BUCKET_NAME:
    raise EnvironmentError(
        "Missing required environment variable: GCS_BUCKET_NAME")

# MongoDB CONFIG
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "ReN")
if not MONGO_URI:
    raise EnvironmentError("Missing required environment variable: MONGO_URI")

# Lazy, per-process MongoDB client.
# A global client created at import time is inherited by Celery fork-workers with
# a live TLS socket — OpenSSL state is not fork-safe and causes SIGSEGV (signal 11).
# By tracking the PID we ensure each forked worker creates its own connection.
_mongo_client = None
_mongo_pid: Optional[int] = None


def _get_collections():
    global _mongo_client, _mongo_pid
    pid = os.getpid()
    if _mongo_client is None or _mongo_pid != pid:
        if _mongo_client is not None:
            try:
                _mongo_client.close()
            except Exception:
                pass
        _mongo_client = MongoClient(MONGO_URI, tls=True, tlsCAFile=certifi.where())
        _mongo_pid = pid
    db = _mongo_client[MONGO_DB]
    return db["failed_urls"], db["extraction_log_reports"]


def _failed_urls_col():
    return _get_collections()[0]


def _extraction_logs_col():
    return _get_collections()[1]


# Scopes for Cloud Storage. You may also use cloud-platform if you prefer.
STORAGE_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/devstorage.read_write"
    # or: "https://www.googleapis.com/auth/cloud-platform"
]

# Default timeouts (seconds) for GCS operations
GCS_DOWNLOAD_TIMEOUT = int(os.getenv("GCS_DOWNLOAD_TIMEOUT", "60"))
GCS_UPLOAD_TIMEOUT = int(os.getenv("GCS_UPLOAD_TIMEOUT", "120"))

# ──────────────────────────────────────────────────────────────────────────────
# GCS Client Factory (per call / per process) – DO NOT use a global Client
# ──────────────────────────────────────────────────────────────────────────────


# 👇 NEW: helper to store a compact extraction report per task/company
def save_extraction_log(report: dict) -> None:
    """
    Insert a high-level extraction report document into MongoDB.

    Expected keys in report (not all mandatory but recommended):
      - task_id
      - section
      - company_name
      - uid
      - started_at (unix ts)
      - finished_at (unix ts)
      - duration_sec
      - soft_time_limit_hit (bool)
      - counters: {input_rows, candidate_pdfs, stored_files, failed_files}
      - errors_sample: [str, ...]   # short list of error messages
      - source_s3_key               # optional, from the XLS
    """
    report = dict(report)  # shallow copy, avoid mutating caller

    # Basic safety: always stamp a timestamp if missing
    report.setdefault("timestamp", int(time.time()))

    try:
        _extraction_logs_col().insert_one(report)
        logging.info(
            "Saved extraction log for company=%s task_id=%s",
            report.get("company_name"), report.get("task_id"),
        )
    except Exception as e:
        logging.error("Error saving extraction log to MongoDB: %s", e)


def make_storage_client() -> storage.Client:
    """
    Create a Cloud Storage client with explicit scopes.
    Prefer a service account key if GOOGLE_APPLICATION_CREDENTIALS is set;
    otherwise fall back to ADC but still force the scopes.
    """
    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path and os.path.exists(sa_path):
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=STORAGE_SCOPES
        )
        return storage.Client(project=creds.project_id, credentials=creds)

    creds, proj = google.auth.default(scopes=STORAGE_SCOPES)
    return storage.Client(project=proj, credentials=creds)


def _log_creds_once(client: storage.Client) -> None:
    """
    Optional: log the principal + scopes for quick debugging.
    Call this once per worker (e.g., on first use).
    """
    try:
        creds = client._credentials  # type: ignore[attr-defined]
        principal = getattr(creds, "service_account_email",
                            None) or getattr(creds, "username", None)
        logging.info("[GCS] principal=%s scopes=%s",
                     principal, getattr(creds, "scopes", None))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# MongoDB Helpers
# ──────────────────────────────────────────────────────────────────────────────

def epoch(dt_str):
    """Convert a date string to epoch seconds (UTC). Kept for any legacy callers.
    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {dt_str!r}")


def get_failed_items(start_date=None, end_date=None):
    """
    Fetch failed-URL records from MongoDB filtered by an optional timestamp range.

    Args:
        start_date: Unix epoch seconds (int or numeric string).
                    If omitted, no lower bound is applied.
        end_date:   Unix epoch seconds (int or numeric string).
                    If omitted, no upper bound is applied — query just returns
                    everything ≥ start_date.

    Raises:
        ValueError if a value is provided but isn't a valid integer timestamp.
    """
    def _coerce(value, label):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"`{label}` must be a Unix timestamp (integer seconds), got {value!r}"
            )

    start_ts = _coerce(start_date, "start_date")
    end_ts = _coerce(end_date, "end_date")

    try:
        # No category filter — return every failed_url record regardless of
        # category. The reingest task applies its own PERMANENT_FAILURE_CATEGORIES
        # filter before dispatching, which is the correct place for that logic.
        query: dict = {}

        timestamp_filter: dict = {}
        if start_ts is not None:
            timestamp_filter["$gte"] = start_ts
        if end_ts is not None:
            timestamp_filter["$lte"] = end_ts

        if timestamp_filter:
            query["timestamp"] = timestamp_filter

        docs = _failed_urls_col().find(query)
        return [sanitize_for_json(doc) for doc in docs]

    except ValueError:
        raise
    except Exception as e:
        logging.error("Error retrieving failed items: %s", e)
        return []


def sanitize_for_json(value):
    """Recursively make Mongo docs JSON-safe for DRF."""
    if isinstance(value, dict):
        return {k: sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            # choose how you want to represent it
            return None  # or str(value) like "NaN" / "Infinity"
    return value


def delete_failed_urls(ids: list) -> int:
    """Delete failed URL documents by their MongoDB ObjectId strings. Returns deleted count."""
    from bson import ObjectId
    try:
        object_ids = [ObjectId(i) for i in ids]
        result = _failed_urls_col().delete_many({"_id": {"$in": object_ids}})
        logging.info("Deleted %d failed URL records", result.deleted_count)
        return result.deleted_count
    except Exception as e:
        logging.error("Error deleting failed URL records: %s", e)
        return 0


def save_failed_url(reason: str, category: str, company_id: str, metadata: dict) -> None:
    """Insert a failed-URL record into MongoDB."""
    if metadata is None:
        logging.warning("save_failed_url called with metadata=None")
        return

    data = {
        "url": metadata.get("Link"),
        "reason of failure": reason,
        "category of failure": category,
        "company_id": company_id,
        "timestamp": int(time.time()),
        "Type of Document": metadata.get("Type of Document"),
        "Sector": metadata.get("Sector"),
        "Industry": metadata.get("Industry"),
        "Company Name": metadata.get("Company Name"),
        "ISIN": metadata.get("ISIN"),
        "Country": metadata.get("Country"),
        "Exchange": metadata.get("Exchange"),
        "Ticker Symbol": metadata.get("Ticker Symbol"),
        "Link": metadata.get("Link"),
        "File Type": metadata.get("File Type"),
        "Year": metadata.get("Year"),
        "ESG Category": metadata.get("ESG Category"),
        "Type of Report": metadata.get("Type of Report"),
        "Shared Drive": metadata.get("Shared Drive"),
        "Title of the Report": metadata.get("Title of the Report"),
    }

    try:
        _failed_urls_col().insert_one(data)
        logging.info("Saved failed URL %s with reason: %s",
                     data["url"], reason)
    except Exception as e:
        logging.error("Error saving to MongoDB: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# GCS File Helpers (download/upload) – with explicit timeouts and scopes
# ──────────────────────────────────────────────────────────────────────────────
def download_file_from_gcs(bucket_name: str, key: str, download_path: str) -> bool:
    """Download a blob from GCS to a local file."""
    client = make_storage_client()
    # _log_creds_once(client)  # uncomment for debugging identity/scopes
    try:
        blob = client.bucket(bucket_name).blob(key)
        blob.download_to_filename(download_path, timeout=GCS_DOWNLOAD_TIMEOUT)
        logging.info("Downloaded %s to %s", key, download_path)
        return True
    except Forbidden as e:
        creds = client._credentials  # type: ignore[attr-defined]
        principal = getattr(creds, "service_account_email", None) or "unknown"
        logging.error(
            "[GCS 403] principal=%s scopes=%s bucket=%s object=%s err=%s",
            principal, getattr(creds, "scopes", None), bucket_name, key, getattr(
                e, "message", e)
        )
        return False
    except NotFound:
        logging.error("GCS object not found: gs://%s/%s", bucket_name, key)
        return False
    except Exception as e:
        logging.error("Error downloading file from GCS: %s", e)
        return False


def download_blob_bytes_from_url_regular(url: str) -> bytes:
    """
    Given a URL like https://storage.googleapis.com/my-bucket/path/to/file.pdf,
    parse (bucket, blob) and download via the GCS SDK.
    """
    p = urlparse(url)
    bucket_name, blob_path = p.path.lstrip("/").split("/", 1)
    client = make_storage_client()
    blob = client.bucket(bucket_name).blob(blob_path)
    return blob.download_as_bytes(timeout=GCS_DOWNLOAD_TIMEOUT)


def download_blob_bytes_from_url(url: str) -> bytes:
    """
    Download bytes directly from Google Cloud Storage URLs only.

    Supports:
      - gs://bucket/path/to/file.pdf
      - https://storage.googleapis.com/bucket/path/to/file.pdf
      - https://storage.cloud.google.com/bucket/path/to/file.pdf

    Raises:
        ValueError: if URL host or scheme is not GCS-related.
    """
    p = urlparse(url)
    host = p.netloc.lower()

    # Acceptable GCS URL patterns
    if p.scheme == "gs":
        bucket_name = p.netloc
        blob_path = unquote(p.path.lstrip("/"))
    elif host in ("storage.googleapis.com", "storage.cloud.google.com"):
        parts = p.path.lstrip("/").split("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Unable to parse bucket & blob from URL: {url}")
        bucket_name, blob_path = parts
        blob_path = unquote(blob_path)
    else:
        raise ValueError(f"Unsupported GCS URL host: {host}")

    # Use GCS SDK for download
    client = make_storage_client()
    blob = client.bucket(bucket_name).blob(blob_path)
    return blob.download_as_bytes(timeout=GCS_DOWNLOAD_TIMEOUT)


def upload_to_gcs(local_path: str, gcs_key: str, metadata_args: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Upload a local file to GCS, setting optional custom metadata, and return (public_url, error).
    """
    try:
        client = make_storage_client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(gcs_key)

        # Optional: ensure metadata values are strings
        if metadata_args:
            blob.metadata = {str(k): (None if v is None else str(v))
                             for k, v in metadata_args.items()}

        blob.upload_from_filename(
            local_path, content_type="application/pdf", timeout=GCS_UPLOAD_TIMEOUT)

        public_url = f"https://storage.googleapis.com/{bucket.name}/{gcs_key}"
        logging.info("Uploaded file: %s → %s", gcs_key, public_url)
        return public_url, None

    except Forbidden as e:
        # Scope/authorization error
        logging.error("Failed to upload file to GCS (403): %s",
                      getattr(e, "message", e))
        return None, str(e)
    except Exception as e:
        err = str(e)
        logging.error("Failed to upload file to GCS: %s", err)
        return None, err


# ──────────────────────────────────────────────────────────────────────────────
# Optional: use an existing Signed URL directly (no OAuth/scopes)
# ──────────────────────────────────────────────────────────────────────────────
def download_via_signed_url(url: str) -> bytes:
    """
    Download using the provided signed URL, preserving its query string.
    Use when you *want* to bypass OAuth and rely on the signature.
    """
    resp = requests.get(url, timeout=GCS_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    return resp.content
