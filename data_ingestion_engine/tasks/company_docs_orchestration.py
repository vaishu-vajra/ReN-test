# data_ingestion_engine/tasks/company_docs_orchestration.py
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from billiard.exceptions import SoftTimeLimitExceeded

from data_ingestion_engine.utils.gc_service import save_extraction_log

logger = logging.getLogger(__name__)


def run_company_docs_task(
    task_self,
    company_rows,
    uid,
    source_s3_key,
    *,
    process_download_fn,
    reschedule_task,
    chunk_rows_fn,
    download_workers,
    link_field="Link",
    label="default",
    section_name="store_docs_for_company_task",
):
    """
    Process all docs for a single company. Same behavior as the original
    store_docs_for_company_task body — see tasks/downloads.py for the
    behavior-preserving wrapper that calls this for the main pipeline.

    Returns {"stored_files": [...], "errors": [...]} — same shape as before.
    """
    task_id = task_self.request.id
    started_at = time.time()
    soft_limit_hit = False

    results = []
    errors = []

    company_name = None
    if company_rows:
        company_name = (company_rows[0].get(
            "Company Name") or "").strip() or None

    # To allow rescheduling remaining docs if we hit soft limit
    remaining_rows = list(company_rows)

    # Manual executor (not a `with` block): on a soft time limit we must NOT let
    # the context-manager exit block waiting for in-flight downloads — that wait
    # is exactly what pushed the old task past the HARD limit into a SIGKILL.
    executor = ThreadPoolExecutor(max_workers=download_workers)
    try:
        future_map = {}
        link_to_row = {}

        for row in company_rows:
            link = row.get(link_field)
            if not link:
                errors.append(f"No {link_field} in row")
                continue

            link_to_row[link] = row
            future = executor.submit(
                process_download_fn, link, company_rows, uid)
            future_map[future] = link

        for future in as_completed(future_map):
            link = future_map[future]
            row = link_to_row.get(link)

            if row in remaining_rows:
                remaining_rows.remove(row)

            try:
                s3_key, error = future.result()
                if s3_key:
                    results.append(s3_key)
                if error:
                    errors.append(f"Error storing doc for {link}: {error}")
            except Exception as e:
                logger.exception(
                    f"[{label}] Unexpected exception for {link}: {str(e)}")
                errors.append(f"Unexpected exception for {link}: {str(e)}")

    except SoftTimeLimitExceeded:
        soft_limit_hit = True
        logger.error(
            f"[{label}] Soft time limit exceeded in {section_name}")
        errors.append("Soft time limit exceeded – partial results only")

        # Abandon in-flight downloads immediately (don't wait) and reschedule the
        # untouched rows in small chunks so nothing is lost.
        executor.shutdown(wait=False, cancel_futures=True)
        if remaining_rows:
            logger.info(
                "[%s] Rescheduling %d remaining docs for company '%s' in chunks due to time limit",
                label, len(remaining_rows), company_name,
            )
            for chunk in chunk_rows_fn(remaining_rows):
                reschedule_task.apply_async(
                    args=[chunk, uid, source_s3_key])

    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        finished_at = time.time()
        duration_sec = finished_at - started_at

        input_rows = len(company_rows)
        candidate_pdfs = sum(
            1 for r in company_rows
            if str(r.get("File Type") or "").strip().upper() == "PDF"
            or str(r.get(link_field) or "").lower().split("?")[0].endswith(".pdf")
        )
        stored_files_count = len(results)
        failed_files_count = len(errors)

        report = {
            "task_id": task_id,
            "section": section_name,
            "label": label,
            "company_name": company_name,
            "uid": uid,
            "source_s3_key": source_s3_key,
            "started_at": int(started_at),
            "finished_at": int(finished_at),
            "duration_sec": duration_sec,
            "soft_time_limit_hit": soft_limit_hit,
            "counters": {
                "input_rows": input_rows,
                "candidate_pdfs": candidate_pdfs,
                "stored_files": stored_files_count,
                "failed_files": failed_files_count,
            },
            "errors_sample": errors[:20],
        }

        save_extraction_log(report)

    return {"stored_files": results, "errors": errors}
