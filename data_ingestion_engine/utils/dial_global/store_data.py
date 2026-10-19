# data_ingestion_engine/utils/dial_global/store_data.py
#
# DataX client for the DIAL Global tables (companies / reports / extractions).
#
# Deliberately separate from utils/store_data.py: the DIAL Global tables have
# their own company_id space (dial_global_companies is NOT the canonical
# `companies` table) and their own endpoints. Nothing here goes through
# store_data()'s section dispatcher.
#
# IMPORTANT — these routers are mounted internal-only on DataX
# (app/api/endpoints/__init__.py: internal_only_routers), so every path lives
# under /internal/dial-global/... and needs an internal JWT. Hitting
# /dial-global/... on the public tree returns 404.
#
# Reports and extractions support server-side filters
# (?company_id=&year=&status=), so scoped lookups are pushed down rather than
# fetched whole. /dial-global/companies/ has no filters, so that one listing is
# still fetched in full behind a short TTL cache.
#
# Every filtered read also re-filters the response client-side. An older DataX
# build ignores unknown query parameters and returns the whole table, which
# would otherwise extract another company's documents or make the extraction
# upsert target another company's row.

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from data_ingestion_engine.utils.store_data import (
    DATAX_INTERNAL,
    _headers,
    _make_json_safe,
    _safe_year,
    _session,
)

logger = logging.getLogger(__name__)

DIAL_GLOBAL_BASE = f"{DATAX_INTERNAL}/dial-global"

# Seconds a full-table listing stays reusable. Short enough that an extraction
# run started right after an ingestion run still sees the new documents.
_LIST_CACHE_TTL = float(os.getenv("DIAL_GLOBAL_LIST_CACHE_TTL", "60"))

_cache_lock = threading.Lock()
_list_cache: Dict[str, tuple] = {}


def _cached_list(key: str, fetch_fn):
    """Memoise a full-table GET for _LIST_CACHE_TTL seconds, per process."""
    now = time.time()
    with _cache_lock:
        hit = _list_cache.get(key)
        if hit and (now - hit[0]) < _LIST_CACHE_TTL:
            return hit[1]

    data = fetch_fn()

    with _cache_lock:
        _list_cache[key] = (time.time(), data)
    return data


def invalidate_dial_global_cache() -> None:
    """Drop the cached listings — call after writing reports or extractions."""
    with _cache_lock:
        _list_cache.clear()


# ---------- Companies ----------


def get_company_id_dial_global(company_name: str, uid: str) -> Optional[int]:
    """
    Upsert a DIAL Global company by name and return its dial_global company_id.

    DataX has no upsert endpoint here — POST /dial-global/companies/ creates and
    returns 409 when the (unique) company_name already exists — so a 409 is
    resolved by looking the name up in the list instead of being an error.

    NOTE: the returned id indexes dial_global_companies. It is NOT the canonical
    `companies.company_id` and must never be passed to utils/store_data.py.
    """
    url = f"{DIAL_GLOBAL_BASE}/companies/"
    payload = _make_json_safe({"company_name": company_name})

    try:
        with _session() as s:
            response = s.post(url, headers=_headers(uid),
                              json=payload, timeout=30)

        if response.status_code == 201:
            invalidate_dial_global_cache()
            return response.json().get("company_id")

        if response.status_code == 409:
            # Already there — resolve the existing row by name.
            existing = find_dial_global_company_by_name(company_name, uid)
            if existing:
                return existing.get("company_id")
            logger.error(
                "DIAL Global company '%s' reported as duplicate but not found in list",
                company_name)
            return None

        logger.error(
            "DIAL Global company upsert failed: %s - %s",
            response.status_code, response.text[:300])
        return None

    except Exception as e:
        logger.error("Error in DIAL Global company upsert: %s", e)
        return None


def fetch_dial_global_companies(uid: str) -> List[Dict[str, Any]]:
    """Every company in dial_global_companies. Cached for _LIST_CACHE_TTL."""

    def _fetch():
        try:
            with _session() as s:
                response = s.get(
                    f"{DIAL_GLOBAL_BASE}/companies/", headers=_headers(uid), timeout=60)
            if response.status_code == 200:
                return response.json() or []
            logger.error(
                "Listing DIAL Global companies failed: %s - %s",
                response.status_code, response.text[:300])
            return []
        except Exception as e:
            logger.error("Error listing DIAL Global companies: %s", e)
            return []

    return _cached_list("companies", _fetch)


def find_dial_global_company_by_name(company_name: str, uid: str) -> Optional[Dict[str, Any]]:
    """Case-insensitive lookup over the company list."""
    target = (company_name or "").strip().lower()
    if not target:
        return None
    # Bypass any stale cache: this is called right after a 409, so the row may
    # have been created by another worker moments ago.
    invalidate_dial_global_cache()
    for company in fetch_dial_global_companies(uid):
        if str(company.get("company_name", "")).strip().lower() == target:
            return company
    return None


def list_dial_global_company_ids(uid: str) -> List[int]:
    """Sorted dial_global company IDs — the fan-out view's work list."""
    ids = []
    for company in fetch_dial_global_companies(uid):
        cid = company.get("company_id")
        if cid is not None:
            ids.append(int(cid))
    return sorted(set(ids))


# ---------- Reports ----------


def store_report_dial_global(company_id: int, document_data: dict, uid: str) -> Optional[int]:
    """
    Create a dial_global_reports row and return its report_id.

    The payload keys must match DialGlobalReportCreate on DataX exactly
    (company_id / document_type / year / document_url). Extra keys are silently
    dropped by Pydantic, so a misnamed key means a NULL column, not an error.
    """
    url = f"{DIAL_GLOBAL_BASE}/reports/"
    payload = _make_json_safe({
        "company_id": int(company_id),
        "document_type": document_data.get("document_type") or "report",
        "year": _safe_year(document_data.get("year"), default=None),
        "document_url": document_data.get("document_url") or document_data.get("gcs_url"),
    })

    try:
        with _session() as s:
            response = s.post(url, headers=_headers(uid),
                              json=payload, timeout=60)

        if response.status_code == 201:
            invalidate_dial_global_cache()
            return response.json().get("report_id")

        logger.error(
            "DIAL Global report store failed: %s - %s",
            response.status_code, response.text[:300])
        return None

    except Exception as e:
        logger.error("Error storing DIAL Global report: %s", e)
        return None


def _get_list(path: str, uid: str, params: Optional[Dict[str, Any]] = None,
              timeout: int = 60) -> List[Dict[str, Any]]:
    """GET a DIAL Global list endpoint with optional server-side filters."""
    query = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        with _session() as s:
            response = s.get(f"{DIAL_GLOBAL_BASE}{path}", headers=_headers(uid),
                             params=query or None, timeout=timeout)
        if response.status_code == 200:
            return response.json() or []
        logger.error(
            "Listing %s failed: %s - %s",
            path, response.status_code, response.text[:300])
        return []
    except Exception as e:
        logger.error("Error listing %s: %s", path, e)
        return []


def fetch_all_dial_global_reports(uid: str) -> List[Dict[str, Any]]:
    """
    Every row in dial_global_reports, cached for _LIST_CACHE_TTL.

    Only for callers that genuinely want the whole table. Anything scoped to one
    company should use fetch_dial_global_reports_for_company(), which filters
    server-side.
    """
    return _cached_list("reports", lambda: _get_list("/reports/", uid))


def fetch_dial_global_reports_for_company(
    company_id: int,
    uid: str,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Documents belonging to one DIAL Global company, newest year first.

    `company_id` is a dial_global_companies id. When `year` is given, only
    documents for that reporting year are returned. Both are pushed down to
    DataX as query parameters (`?company_id=&year=`) rather than filtered here.

    Returns a list (empty when there is nothing), not the {"reports": [...],
    "filings": [...]} envelope the canonical fetch_reports_for_company returns —
    the DIAL Global reports table has no such buckets.
    """
    try:
        target = int(company_id)
    except (TypeError, ValueError):
        logger.error("Invalid DIAL Global company_id: %r", company_id)
        return []

    docs = _get_list("/reports/", uid,
                     {"company_id": target,
                      "year": int(year) if year is not None else None})

    # Defensive: an older DataX build ignores unknown query params and returns
    # the whole table, which would silently extract other companies' documents.
    docs = [d for d in docs
            if d.get("company_id") is not None and int(d["company_id"]) == target]
    if year is not None:
        docs = [d for d in docs if d.get("year") is not None
                and int(d["year"]) == int(year)]

    # Newest first so a partial run covers the most relevant years.
    docs.sort(key=lambda d: (d.get("year") or 0), reverse=True)
    return docs


# ---------- Extractions ----------

# dial_global_extractions.status — "scored" once the CITIES scoring pass has
# run, "unscored" when only the pass-1 extraction survived.
STATUS_SCORED = "scored"
STATUS_UNSCORED = "unscored"


def fetch_all_dial_global_extractions(uid: str) -> List[Dict[str, Any]]:
    """
    Every row in dial_global_extractions, cached for _LIST_CACHE_TTL.

    Only the leaderboard export wants the whole table. Scoped lookups should use
    fetch_dial_global_extractions(), which filters server-side.
    """
    return _cached_list(
        "extractions", lambda: _get_list("/extractions/", uid, timeout=120))


def fetch_dial_global_extractions(
    uid: str,
    company_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[str] = None,
    company_field: str = "company_id",
) -> List[Dict[str, Any]]:
    """
    Extraction rows matching the given filters, newest extraction_id first.

    Filters are pushed down to DataX as `?company_id=&year=&status=`.

    `company_field` selects which company column to match on — "company_id" for
    DIAL Global, "ren_company_id" for ReN. The two index different tables and
    must never be confused for one another.
    """
    rows = _get_list("/extractions/", uid, {
        company_field: int(company_id) if company_id is not None else None,
        "year": int(year) if year is not None else None,
        "status": status,
    }, timeout=120)

    # Defensive: an older DataX build ignores unknown query params and returns
    # the whole table, which would make the upsert below target another
    # company's record.
    if company_id is not None:
        rows = [r for r in rows if r.get(company_field) is not None
                and int(r[company_field]) == int(company_id)]
    if year is not None:
        rows = [r for r in rows if r.get("year") is not None
                and int(r["year"]) == int(year)]
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]

    rows.sort(key=lambda r: (r.get("extraction_id") or 0), reverse=True)
    return rows


def store_dial_global_extraction(
    company_id: int,
    year: int,
    extracted_data: Any,
    uid: str,
    source_report_ids: Optional[List[int]] = None,
    status: str = STATUS_SCORED,
    company_field: str = "company_id",
) -> Optional[Dict[str, Any]]:
    """
    Persist one company-year extraction result.

    `extracted_data` may be a Pydantic model or a plain dict; it lands in the
    JSONB column dial_global_extractions.extracted_data.

    `source_report_ids` lists every report that fed the model call. It carries no
    foreign key on the DataX side by design — that is what stops a company-year
    extraction being cascade-deleted when one of its source documents is removed.
    `report_id` is deliberately NOT sent: setting it would re-expose the row to
    exactly that cascade.

    `company_field` selects the universe: "company_id" for DGI companies
    (dial_global_companies), "ren_company_id" for ReN companies (the canonical
    companies table). DataX enforces exactly one of the two per row.

    `status` is "scored" when the CITIES scoring pass succeeded and "unscored"
    when we are falling back to the extraction alone.

    A single POST does the whole job: DataX upserts on (company, year) against a
    partial unique index, so a re-run updates in place rather than duplicating,
    and concurrent workers on the same company-year cannot both insert. 201 means
    a new row, 200 means an existing one was updated — BOTH are success.

    DataX also refuses to let an unscored result overwrite a scored one, and
    answers a suppressed downgrade with 200 and the preserved scored record. That
    shows up here as `downgrade_suppressed` in the returned dict.

    Returns the stored record — the DataX row plus `created` and
    `downgrade_suppressed` — or None when nothing was written.

    NOTE: always pass a year. Rows without one fall outside the unique indexes
    and are inserted every time, so they would accumulate duplicates.
    """
    if extracted_data is None:
        logger.warning(
            "No extraction data for %s=%s year=%s — nothing stored",
            company_field, company_id, year)
        return None

    if hasattr(extracted_data, "model_dump"):
        extracted_data = extracted_data.model_dump(mode="json")

    body = _make_json_safe({
        company_field: int(company_id),
        "year": int(year),
        "source_report_ids": [int(r) for r in (source_report_ids or [])],
        "extracted_data": extracted_data,
        "status": status,
    })

    try:
        with _session() as s:
            response = s.post(
                f"{DIAL_GLOBAL_BASE}/extractions/",
                headers=_headers(uid),
                json=body,
                timeout=120,
            )

        if response.status_code not in (200, 201):
            # 409 is a constraint violation — most likely a company id that does
            # not exist in the universe named by company_field.
            logger.error(
                "DIAL Global extraction store failed: %s - %s (%s=%s year=%s)",
                response.status_code, response.text[:300],
                company_field, company_id, year)
            return None

        invalidate_dial_global_cache()
        record = response.json() or {}
        record["created"] = response.status_code == 201
        record["downgrade_suppressed"] = (
            status == STATUS_UNSCORED
            and record.get("status") == STATUS_SCORED
        )

        if record["downgrade_suppressed"]:
            logger.info(
                "Kept the scored extraction for %s=%s year=%s — DataX suppressed "
                "the unscored downgrade (extraction_id=%s)",
                company_field, company_id, year, record.get("extraction_id"))
        else:
            logger.info(
                "%s extraction id=%s %s=%s year=%s status=%s sources=%s",
                "Stored" if record["created"] else "Updated",
                record.get("extraction_id"), company_field, company_id, year,
                record.get("status"), body["source_report_ids"])

        return record

    except Exception as e:
        logger.error("Error storing DIAL Global extraction: %s", e)
        return None
