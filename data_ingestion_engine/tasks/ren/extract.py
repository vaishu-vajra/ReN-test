# data_ingestion_engine/tasks/ren/extract.py
#
# The ReN extraction pipeline.
#
# Same two-pass CITIES engine as DIAL Global — same schemas, same prompts, same
# scoring — driven from a different source. This module owns only the
# differences:
#
#   * documents come from the canonical companies / company-documents tables via
#     fetch_reports_for_company(), so company_id is a canonical companies id
#   * documents are SELECTED rather than taken wholesale: an Annual, DEI or
#     Sustainability report anchors the year, and the others join it when
#     present (see utils/ren/document_selection.py)
#   * only the most recent `years` company-years are run, default 2
#   * results are keyed on ren_company_id (see utils/ren/store_data.py)

import logging
import os
from typing import Any, Dict, Optional

from celery import shared_task

from data_ingestion_engine.tasks.company_year_engine import run_company_year_extraction
from data_ingestion_engine.utils.ren.document_selection import (
    select_all_ren_documents,
    select_ren_documents,
)
from data_ingestion_engine.utils.ren.store_data import store_ren_extraction
from data_ingestion_engine.utils.ren.company_group import (
    fetch_reports_for_company_group,
)

logger = logging.getLogger(__name__)

# Its own queue so a ReN backlog (~1,100 companies) cannot starve the DIAL
# Global one, and so the two can be scaled independently. Must also be added to
# the worker's -Q args (k8s/base/celery-deployment.yaml).
REN_QUEUE = "ren"

DEFAULT_YEARS = 2


@shared_task(bind=True, rate_limit="6/m", soft_time_limit=1800, time_limit=25000,
             queue=REN_QUEUE)
def extract_ren_for_company(
    self,
    company_id: int,
    uid: str,
    year: Optional[int] = None,
    years: int = DEFAULT_YEARS,
    company_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run the two-pass CITIES engine over one ReN company's recent company-years.

    Args:
        company_id: a canonical companies.company_id.
        uid: user id, minted into the internal JWT.
        year: restrict to a single reporting year, ignoring `years`.
        years: how many recent reporting years to run, most recent first.
            Only years holding an anchor document count.
        company_name: for the extraction log; purely informational.

    Returns {company_id, results, errors, year}.
    """
    # Deliberately unfiltered by year: selection needs to see every year before
    # it can pick the most recent `years` that actually have an annual report.
    #
    # Read the whole name group, not just this company_id. The same business is
    # often ingested under several entities and its filings scatter across them,
    # so one year's annual report can sit under a different row than the next
    # year's. See utils/ren/company_group.py.
    payload = fetch_reports_for_company_group(company_id, uid)
    docs = select_ren_documents(payload, years=years, year=year)

    if not docs:
        logger.info(
            "[ren] No usable documents for company_id=%s (a company-year needs "
            "an Annual, DEI or Sustainability report to anchor it)", company_id)

    return run_company_year_extraction(
        task_id=self.request.id,
        company_id=company_id,
        uid=uid,
        docs=docs,
        store_fn=store_ren_extraction,
        label="ren",
        section_name="extract_ren_for_company",
        year=year,
        company_name=company_name,
    )


# soft_time_limit is 3x the joint task's. Progressive rounds run one after the
# other — each needs the previous round's residual before it can be built — so a
# company-year holding ten documents is ten sequential model calls inside one
# task. Measured call latency is median 5.5s / p99 118s, so ten rounds has a
# realistic tail well past the 1800s the joint task allows.
PROGRESSIVE_SOFT_TIME_LIMIT = int(
    os.getenv("REN_PROGRESSIVE_SOFT_TIME_LIMIT", "5400"))


@shared_task(bind=True, rate_limit="6/m", soft_time_limit=PROGRESSIVE_SOFT_TIME_LIMIT,
             time_limit=25000, queue=REN_QUEUE)
def extract_ren_progressive_for_company(
    self,
    company_id: int,
    uid: str,
    year: Optional[int] = None,
    years: int = DEFAULT_YEARS,
    company_name: Optional[str] = None,
    max_per_type: Optional[int] = None,
    max_per_year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run the CITIES engine over one ReN company-year, one document per call.

    Differs from extract_ren_for_company in exactly two places:

      * selection is select_all_ren_documents() — eleven report types instead of
        four, and EVERY document of each type rather than the newest one
      * extraction is progressive — each document gets its own model call and is
        asked only the questions no earlier document answered

    Everything after pass 1 is identical: same rubric, same scoring pass, same
    storage, same (ren_company_id, year) upsert.

    Normally dispatched per (company_id, year) pair rather than per company: the
    cohort this was built for is a list of specific company-years, and a single
    year per task keeps each unit of work small enough to retry cheaply.

    Args:
        company_id: a canonical companies.company_id.
        uid: user id, minted into the internal JWT.
        year: restrict to a single reporting year, ignoring `years`.
        years: how many recent reporting years to run when `year` is None.
        company_name: for the extraction log; purely informational.
        max_per_type: documents of one type per year; None uses
            REN_MAX_DOCS_PER_TYPE (0 = unlimited).
        max_per_year: hard cap on documents per year; None uses
            REN_MAX_DOCS_PER_YEAR.

    Returns {company_id, results, errors, year}.
    """
    payload = fetch_reports_for_company_group(company_id, uid)
    docs = select_all_ren_documents(
        payload, years=years, year=year,
        max_per_type=max_per_type, max_per_year=max_per_year)

    if not docs:
        logger.info(
            "[ren] No usable documents for company_id=%s year=%s across the "
            "eleven progressive report types", company_id, year)
    else:
        logger.info(
            "[ren] company_id=%s year=%s: %s document(s) to read — %s",
            company_id, year, len(docs),
            [d.get("document_type") for d in docs])

    return run_company_year_extraction(
        task_id=self.request.id,
        company_id=company_id,
        uid=uid,
        docs=docs,
        store_fn=store_ren_extraction,
        label="ren-progressive",
        section_name="extract_ren_progressive_for_company",
        year=year,
        company_name=company_name,
        progressive=True,
    )
