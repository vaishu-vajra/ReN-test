# data_ingestion_engine/tasks/dial_global/extract.py
#
# The DIAL Global (DGI-collected) extraction pipeline.
#
# This module owns only what is specific to DIAL Global: where documents come
# from and where results go. The two-pass engine itself — company-year grouping,
# both model calls, the curated-year stamping, the scoring arithmetic and the
# log record — lives in tasks/company_year_engine.py and is shared with the ReN
# pipeline.
#
# Documents come from the dial_global_* tables. The company_id here indexes
# dial_global_companies, NOT the canonical `companies` table.

import logging
import os
from typing import Any, Dict, List, Optional

from celery import shared_task

from data_ingestion_engine.tasks.company_year_engine import run_company_year_extraction
from data_ingestion_engine.tasks.dial_global.dial_global_downloads import DIAL_GLOBAL_QUEUE
from data_ingestion_engine.utils.dial_global.store_data import (
    fetch_dial_global_reports_for_company,
    store_dial_global_extraction,
)
from data_ingestion_engine.utils.ren.document_selection import (
    REN_ROUND_ORDER,
    canonical_report_type,
)

logger = logging.getLogger(__name__)

# soft_time_limit for the progressive task. Rounds run one after another, so a
# company-year holding several report types is several sequential model calls
# inside one task — the same reasoning as REN_PROGRESSIVE_SOFT_TIME_LIMIT.
DG_PROGRESSIVE_SOFT_TIME_LIMIT = int(
    os.getenv("DG_PROGRESSIVE_SOFT_TIME_LIMIT", "5400"))


def order_for_progressive(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort a DIAL Global document set into progressive round order.

    The engine groups by `document_type` in ARRIVAL order, so ordering here is
    what decides the rounds. Types are mapped onto REN_ROUND_ORDER through
    canonical_report_type() so both universes read their evidence in the same
    sequence — broadest source first, single-topic policies last.

    Nothing is DROPPED. That is the difference from the ReN rule, and it is
    deliberate: DIAL Global ingests a hand-curated document set, so a type the
    canonical patterns do not recognise is still evidence somebody chose to
    ingest. Unrecognised types keep their original relative order and run last.
    """
    order = {label: index for index, label in enumerate(REN_ROUND_ORDER)}
    unknown = len(order)

    def key(item):
        index, doc = item
        canonical = canonical_report_type(doc.get("document_type"))
        # Group unrecognised types together by their raw label rather than
        # scattering them, so each still gets exactly one round.
        rank = order.get(canonical, unknown)
        label = canonical or str(doc.get("document_type") or "")
        return (rank, label, -(doc.get("report_id") or 0), index)

    return [doc for _, doc in sorted(enumerate(docs), key=key)]


@shared_task(bind=True, rate_limit="6/m", soft_time_limit=1800, time_limit=25000,
             queue=DIAL_GLOBAL_QUEUE)
def extract_dial_global_for_company(
    self,
    company_id: int,
    uid: str,
    year: Optional[int] = None,
    document_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run the two-pass engine over every company-year of one DIAL Global company.

    A company with 2023 and 2024 documents produces two extraction JSONs and two
    scores — one per year — each built from all of that year's documents at once.

    Args:
        company_id: a dial_global_companies id.
        uid: user id, minted into the internal JWT.
        year: optional filter to a single reporting year.
        document_types: optional whitelist, e.g. ["Annual Report"]. Default is
            every document the company has for the year — DIAL Global ingests a
            curated document set, so unlike ReN there is nothing to select down.

    Returns {company_id, results, errors, year}.
    """
    docs = fetch_dial_global_reports_for_company(company_id, uid, year=year)

    if document_types:
        wanted = {str(t).strip().lower() for t in document_types}
        docs = [d for d in docs if str(
            d.get("document_type", "")).strip().lower() in wanted]

    docs = [d for d in docs if d.get("document_url")]

    return run_company_year_extraction(
        task_id=self.request.id,
        company_id=company_id,
        uid=uid,
        docs=docs,
        store_fn=store_dial_global_extraction,
        label="dial_global",
        section_name="extract_dial_global_for_company",
        year=year,
    )


@shared_task(bind=True, rate_limit="6/m",
             soft_time_limit=DG_PROGRESSIVE_SOFT_TIME_LIMIT, time_limit=25000,
             queue=DIAL_GLOBAL_QUEUE)
def extract_dial_global_progressive_for_company(
    self,
    company_id: int,
    uid: str,
    year: Optional[int] = None,
    document_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run the PROGRESSIVE engine over one DIAL Global company's company-years.

    Identical sourcing and storage to extract_dial_global_for_company; the only
    difference is how the year's documents are read. The joint task puts them
    all in one model call and caps the call at MAX_DOCS_PER_CALL (6), silently
    discarding everything past the sixth. That cap is not a curation choice —
    it is the context window — and it is why a company-year holding 19 DEI
    reports currently gets 6 of them read.

    Progressive puts one REPORT TYPE in each call and carries the unanswered
    questions forward, so breadth costs sequential calls rather than dropped
    documents. Every document of a type is read together as one body of
    evidence, bounded per call by DIAL_GLOBAL_MAX_DOCS_PER_ROUND.

    Args:
        company_id: a dial_global_companies id.
        uid: user id, minted into the internal JWT.
        year: optional filter to a single reporting year.
        document_types: optional whitelist of raw stored types.

    Returns {company_id, results, errors, year}.
    """
    docs = fetch_dial_global_reports_for_company(company_id, uid, year=year)

    if document_types:
        wanted = {str(t).strip().lower() for t in document_types}
        docs = [d for d in docs if str(
            d.get("document_type", "")).strip().lower() in wanted]

    docs = [d for d in docs if d.get("document_url")]
    docs = order_for_progressive(docs)

    if docs:
        logger.info(
            "[dial_global] company_id=%s year=%s: %s document(s), types in "
            "round order: %s", company_id, year, len(docs),
            list(dict.fromkeys(d.get("document_type") for d in docs)))

    return run_company_year_extraction(
        task_id=self.request.id,
        company_id=company_id,
        uid=uid,
        docs=docs,
        store_fn=store_dial_global_extraction,
        label="dial_global-progressive",
        section_name="extract_dial_global_progressive_for_company",
        year=year,
        progressive=True,
    )
