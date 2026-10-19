# data_ingestion_engine/tasks/dial_global/downloads.py
import os
import logging

from celery import shared_task

from data_ingestion_engine.tasks.company_docs_orchestration import run_company_docs_task
from data_ingestion_engine.tasks.downloads import chunk_rows, DOWNLOAD_WORKERS

logger = logging.getLogger(__name__)

# Dedicated Celery queue so a DIAL Global backlog cannot starve the main
# ingestion queue, and so the HPA can scale the two independently (see
# k8s/SCALING_RUNBOOK.md — queue must also be added to the worker -Q args there).
DIAL_GLOBAL_QUEUE = "dial_global"


LINK_FIELD = "Document URL (Google drive)"


def _process_download_dial_global(link, company_rows, uid):
    """
    Adapter between run_company_docs_task and process_download_dial_global.

    The orchestrator hands out (link, company_rows, uid) and expects
    (stored_key_or_None, error_or_None); process_download_dial_global takes
    (company_name, row, uid) and returns a result dict. This bridges the two so
    the shared orchestration can drive the DIAL Global download path unchanged.
    """
    from data_ingestion_engine.utils.dial_global.process_download import (
        process_download_dial_global,
    )

    row = next((r for r in company_rows if r.get(LINK_FIELD) == link), None)
    if row is None:
        return None, f"No row found for link {link}"

    company_name = (row.get("Company Name") or "").strip()
    if not company_name:
        return None, f"No company name on row for {link}"

    try:
        result = process_download_dial_global(company_name, row, uid) or {}
    except Exception as e:
        logger.exception("[dial_global] Download failed for %s", link)
        return None, str(e)

    if result.get("report_id") is None:
        return None, f"Stored no report for {link}: {result}"

    return result.get("gcs_url"), None


@shared_task(bind=True, soft_time_limit=800, time_limit=1500, queue=DIAL_GLOBAL_QUEUE)
def store_docs_for_company_task_dial_global(self, company_rows, uid, source_s3_key=None):
   
    return run_company_docs_task(
        self,
        company_rows,
        uid,
        source_s3_key,
        process_download_fn=_process_download_dial_global,
        reschedule_task=store_docs_for_company_task_dial_global,
        chunk_rows_fn=chunk_rows,
        download_workers=DOWNLOAD_WORKERS,
        link_field=LINK_FIELD,
        label="dial_global",
        section_name="store_docs_for_company_task_dial_global",
    )
