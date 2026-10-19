import time
from celery.exceptions import SoftTimeLimitExceeded
import os
import shutil
import logging
from collections import defaultdict
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task
from data_ingestion_engine.utils.gc_service import (
    save_extraction_log, save_failed_url,
    get_failed_items,
)
from ..utils.process_download import process_download  # keep your existing helper
from .company_docs_orchestration import run_company_docs_task

logger = logging.getLogger(__name__)

# Max documents processed per Celery task. Large companies (e.g. an FCA filing
# history with hundreds of docs) are split into several small tasks so no single
# task can approach the worker time limit. Tunable via env.
MAX_DOCS_PER_TASK = int(os.getenv("MAX_DOCS_PER_TASK", "20"))

# Threads per task for parallel downloads. Each HTML/office/ESEF doc can spawn a
# headless Chromium or LibreOffice, so effective concurrency per pod is
# (Celery --concurrency) x DOWNLOAD_WORKERS. Keep this in line with the node's
# CPU/RAM to avoid OOM — tunable via env so it can be adjusted without a redeploy.
DOWNLOAD_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "4"))


def chunk_rows(rows, size=None):
    """Yield successive `size`-length chunks of `rows`."""
    size = size or MAX_DOCS_PER_TASK
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


@shared_task(soft_time_limit=700, time_limit=15000)
def download_files_batch_task(urls, user_id, json_data):
    """
    Process a list of URLs by downloading and uploading them to S3.
    Returns { downloaded_files: [s3_keys], errors: [str] }.
    """
    downloaded_files = []
    errors = []

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        future_to_url = {
            executor.submit(process_download, url, user_id, json_data): url
            for url in urls
        }

        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
                if result:
                    s3_key, error = result
                    if s3_key:
                        downloaded_files.append(s3_key)
                    if error:
                        errors.append(f"Error processing {url}: {error}")
            except SoftTimeLimitExceeded:
                err_msg = "Timeout exceeded"
                errors.append(f"Timeout exceeded processing {url}")
                # Retrieve metadata for this URL
                metadata = next(
                    (item for item in json_data if item['Link'] == url), None)
                if not metadata:
                    raise Exception(f"No metadata found for URL: {url}")
                save_failed_url(
                    reason=str(err_msg),
                    category="Failed URLs",
                    company_id=0,
                    metadata=metadata,
                )
            except Exception as e:
                errors.append(f"Unexpected error processing {url}: {str(e)}")

        if os.path.exists('pdfs'):
            shutil.rmtree('pdfs')
            logger.info("Cleaned up the 'pdfs' directory.")

    return {'downloaded_files': downloaded_files, 'errors': errors}


@shared_task(bind=True, soft_time_limit=800, time_limit=1500)
def store_docs_for_company_task(self, company_rows, uid, source_s3_key=None):
    """
    Process all docs for a single company (main pipeline).

    Behavior-preserving wrapper: the actual chunking / ThreadPoolExecutor /
    soft-time-limit-reschedule / extraction-log logic now lives in
    company_docs_orchestration.run_company_docs_task, shared with the DIAL
    Global pipeline's task (DIAL-1 Task 4). This function's inputs, outputs,
    and behavior are unchanged from before the refactor.
    """
    return run_company_docs_task(
        self,
        company_rows,
        uid,
        source_s3_key,
        process_download_fn=process_download,
        reschedule_task=store_docs_for_company_task,
        chunk_rows_fn=chunk_rows,
        download_workers=DOWNLOAD_WORKERS,
        link_field="Link",
        label="default",
        section_name="store_docs_for_company_task",
    )


# File extensions that can appear as TLDs when a bare filename is prefixed with
# "https://" by the ingestion normaliser — these are not real hostnames.
_FILE_EXT_TLDS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "ppt", "pptx"}

# Failure categories that will never succeed on retry without human intervention.
# URLs in these categories are kept in MongoDB for visibility but not re-queued.
# Categories MUST match the `.category` attribute on exception classes:
#   - DownloadError tree in utils/download_file.py
#   - ProcessingError tree in utils/process_download.py
PERMANENT_FAILURE_CATEGORIES = frozenset({
    "HTML Page URL",       # URL is a web page, not a direct PDF link
    "Server Protected",    # CDN/DRM returns non-PDF binary — needs auth token
    "HTTP 403",            # Hard block — server explicitly denies access
    "HTTP 404",            # Resource gone — won't come back on retry
    "Invalid URL",         # Malformed / unresolvable URL
    "Invalid PDF",         # File downloaded but is corrupt — re-download yields same bytes
    "Metadata Missing",    # No matching row in source data — caller-side problem
    "File Too Large",      # Exceeds RAG-indexing size cap — same file will always be too big
})


def _is_reingestionable_url(url: str) -> bool:
    """Return False for URLs that are clearly broken (e.g. bare filename treated as domain)."""
    if not url:
        return False
    try:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return False
        tld = p.netloc.rsplit(".", 1)[-1].lower()
        if tld in _FILE_EXT_TLDS:
            return False
        return True
    except Exception:
        return False


def _doc_to_row(doc: dict) -> dict:
    """Map a failed_urls MongoDB document back to the metadata row shape."""
    return {
        "Link": doc.get("Link") or doc.get("url"),
        "Company Name": doc.get("Company Name"),
        "Type of Document": doc.get("Type of Document"),
        "Sector": doc.get("Sector"),
        "Industry": doc.get("Industry"),
        "ISIN": doc.get("ISIN"),
        "Country": doc.get("Country"),
        "Exchange": doc.get("Exchange"),
        "Ticker Symbol": doc.get("Ticker Symbol"),
        "File Type": doc.get("File Type"),
        "Year": doc.get("Year"),
        "ESG Category": doc.get("ESG Category"),
        "Type of Report": doc.get("Type of Report"),
        "Shared Drive": doc.get("Shared Drive"),
        "Title of the Report": doc.get("Title of the Report"),
    }


@shared_task(bind=True, soft_time_limit=3600, time_limit=7200)
def reingest_failed_urls_task(self, ids=None, start_date=None, end_date=None, failed_urls=None):
   
    task_id = self.request.id

    if failed_urls:
        # Caller-provided list — skip the Mongo query entirely.
        logger.info(
            "[reingest] task %s started with %d caller-provided failed_urls (start_date/end_date ignored)",
            task_id, len(failed_urls),
        )
        failed_docs = failed_urls
        source = "request"
    else:
        logger.info(
            "[reingest] task %s started (start_date=%s, end_date=%s, ids=%s)",
            task_id, start_date, end_date, "yes" if ids else "no",
        )
        failed_docs = get_failed_items(start_date=start_date, end_date=end_date)
        source = "mongodb"

    if not failed_docs:
        logger.info("[reingest] no failed URLs to process (source=%s)", source)
        return {
            "source": source,
            "queued_companies": 0,
            "queued_urls": 0,
            "skipped_broken": 0,
            "skipped_permanent": 0,
            "start_date": start_date,
            "end_date": end_date,
        }

    if ids and source == "mongodb":
        id_set = set(ids)
        failed_docs = [d for d in failed_docs if d.get("_id") in id_set]

    valid_rows = []
    broken = []
    permanent = []

    for doc in failed_docs:
        url = doc.get("Link") or doc.get("url")
        category = doc.get("category of failure", "Failed URLs")

        # Skip permanently-failed categories — retrying won't help.
        if category in PERMANENT_FAILURE_CATEGORIES:
            logger.info("[reingest] skipping permanent failure (%s): %s", category, url)
            permanent.append({"url": url, "category": category})
            continue

        # Skip structurally broken URLs.
        if not _is_reingestionable_url(url):
            logger.warning("[reingest] skipping broken URL: %s", url)
            broken.append(url)
            continue

        valid_rows.append((doc["_id"], _doc_to_row(doc)))

    if broken:
        logger.info("[reingest] skipped %d broken URLs (no valid domain)", len(broken))
    if permanent:
        logger.info("[reingest] skipped %d permanently-failed URLs", len(permanent))

    if not valid_rows:
        logger.info("[reingest] nothing retryable after filtering")
        return {
            "queued_companies": 0,
            "queued_urls": 0,
            "skipped_broken": len(broken),
            "skipped_permanent": len(permanent),
        }

    # NOTE: we deliberately do NOT delete failed_url records before retrying.
    # Previous design deleted them upfront and relied on save_failed_url to re-write
    # on failure — but worker crashes / soft-time-limit hits between delete and retry
    # would silently lose records.
    # Trade-off: on a successful retry the old failure doc stays behind. Callers should
    # query the most-recent timestamp per URL when looking at "current" failures.
    rows = [row for _, row in valid_rows]

    company_map = defaultdict(list)
    for row in rows:
        company = str(row.get("Company Name") or "").strip() or "unknown"
        company_map[company].append(row)

    dispatched = {}
    for company_name, company_rows in company_map.items():
        chunk_ids = []
        for chunk in chunk_rows(company_rows):
            res = store_docs_for_company_task.apply_async(
                args=[chunk, "reingest_system", "reingest"]
            )
            chunk_ids.append(res.id)
        dispatched[company_name] = chunk_ids
        logger.info(
            "[reingest] dispatched %d task(s) for '%s' (%d URLs)",
            len(chunk_ids), company_name, len(company_rows),
        )

    logger.info(
        "[reingest] done — %d companies queued, %d URLs, %d broken skipped, %d permanent skipped",
        len(dispatched), len(rows), len(broken), len(permanent),
    )
    return {
        "source": source,
        "queued_companies": len(dispatched),
        "queued_urls": len(rows),
        "skipped_broken": len(broken),
        "skipped_permanent": len(permanent),
        "start_date": start_date,
        "end_date": end_date,
        "task_ids_by_company": dispatched,
    }
