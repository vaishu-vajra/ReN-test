# data_ingestion_engine/api/views/ren/extract_all.py
#
# Fan-out for the ReN CITIES pipeline. Mirrors the DIAL Global `all` endpoint —
# same batching, same Celery group per batch, same 202 response shape — over a
# company cohort.
#
# The cohort comes from one of three places, in order of precedence:
#   1. "company_ids" in the request  — canonical ids, no resolution needed
#   2. "s3_key" in the request       — the cohort spreadsheet in GCS
#   3. REN_COMPANIES                 — the in-code list, for ad-hoc runs

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple, Union

from celery import group
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.ren.companies import REN_COMPANIES
from data_ingestion_engine.tasks.ren.extract import DEFAULT_YEARS, extract_ren_for_company
from data_ingestion_engine.utils.gc_service import download_file_from_gcs
from data_ingestion_engine.utils.ren.company_list import load_cohort, resolve_cohort_row
from data_ingestion_engine.utils.ren.document_selection import select_ren_documents
from data_ingestion_engine.utils.store_data import fetch_reports_for_company

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 40
BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")

# Parallelism for the optional document preflight. These are short DataX reads,
# so the ceiling is DataX's tolerance rather than ours.
DOC_CHECK_WORKERS = int(os.getenv("REN_DOC_CHECK_WORKERS", "8"))

# Parallelism for cohort resolution. Same reasoning as above — short DataX
# reads — but resolution runs on every cohort row rather than only when the
# caller asks for a document check, so it is the step that decides whether the
# request finishes inside gunicorn's 120s worker timeout.
RESOLVE_WORKERS = int(os.getenv("REN_RESOLVE_WORKERS", "16"))


def chunked(seq: List[Any], size: int) -> List[List[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def resolve_cohort(
    companies: List[Dict[str, Any]],
    uid: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Resolve cohort rows to canonical company ids, and never create one.

    Resolution goes through resolve_cohort_row(), which tries ISIN first and
    accepts a match only once it has confirmed that company actually holds
    documents — see the measurements on _RESOLUTION_STRATEGIES.

    A row that matches nothing, and a row that matches a company with no
    documents, both land in `unresolved` with a `reason`. Neither is worth
    spending model calls on, and both used to be indistinguishable from a
    successful resolution because company-upsert created a company on miss.

    Resolution runs concurrently. Every row costs between two and six DataX
    round trips — up to three resolution attempts, each followed by a document
    check — so a 756-row cohort is a few thousand sequential requests when done
    one at a time. That overran gunicorn's 120s worker timeout and killed the
    request before a single company was dispatched. The calls are pure network
    waits, so threads fix it; results are re-sorted afterwards to keep the
    response order stable regardless of which row finishes first.
    """
    resolved: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    def resolve_one(index_and_company):
        index, company = index_and_company
        try:
            outcome = resolve_cohort_row(company, uid)
        except Exception as e:
            logger.error("[ren] Failed to resolve '%s': %s",
                         company.get("company_name"), e)
            outcome = {"company_id": None, "strategy": None,
                       "matched": False, "documents": False}
        return index, company, outcome

    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
        outcomes = list(pool.map(resolve_one, enumerate(companies)))

    for index, company, outcome in sorted(outcomes, key=lambda o: o[0]):
        entry = {
            "company_name": company.get("company_name"),
            "ticker": company.get("ticker"),
            "exchange": company.get("exchange"),
            "isin": company.get("isin"),
        }

        if outcome["documents"]:
            resolved.append({**entry,
                             "company_id": int(outcome["company_id"]),
                             "matched_by": outcome["strategy"]})
        else:
            entry["reason"] = (
                f"matched company_id={outcome['company_id']} "
                f"via {outcome['strategy']}, but it holds no documents"
                if outcome["matched"] else "no matching company")
            unresolved.append(entry)

    return resolved, unresolved


def check_documents(
    resolved: List[Dict[str, Any]],
    uid: str,
    years: int,
    year: Any = None,
) -> Dict[str, Any]:
    """
    Report how many usable company-years each resolved company actually has.

    Every ReN company is expected to already carry ingested documents, so this
    is a mismatch detector rather than a coverage report. A company that
    resolves to an id but has no documents almost certainly means the cohort row
    matched the wrong canonical company — or created a new empty one, since
    company-upsert creates when nothing matches and gives no way to tell the two
    apart from its response. Zero documents is the tell.

    Buckets:
      ready               — has at least one year with an anchor document
      no_usable_documents — has documents, but no year holds an anchor document
                            (an Annual, DEI or Sustainability report)
      no_documents        — nothing at all, i.e. a resolution mismatch
    """
    ready: List[Dict[str, Any]] = []
    no_usable: List[Dict[str, Any]] = []
    no_documents: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    total_company_years = 0

    def _inspect(company: Dict[str, Any]) -> Tuple[Dict[str, Any], str, int]:
        payload = fetch_reports_for_company(company["company_id"], uid)
        raw = []
        if isinstance(payload, dict):
            # All four buckets — `policies` and `others` carry documents the
            # selection rules recognise, so counting only reports+filings
            # under-reports coverage and can call a usable company empty.
            raw = [*(payload.get("reports") or []),
                   *(payload.get("filings") or []),
                   *(payload.get("policies") or []),
                   *(payload.get("others") or [])]
        elif payload:
            raw = list(payload)

        if not raw:
            return company, "no_documents", 0

        docs = select_ren_documents(payload, years=years, year=year)
        company_years = len({d["year"] for d in docs})
        return company, ("ready" if company_years else "no_usable_documents"), company_years

    with ThreadPoolExecutor(max_workers=DOC_CHECK_WORKERS) as pool:
        futures = {pool.submit(_inspect, c): c for c in resolved}
        for future in as_completed(futures):
            company = futures[future]
            try:
                company, bucket, company_years = future.result()
            except Exception as e:
                logger.error("[ren] Document check failed for company_id=%s: %s",
                             company.get("company_id"), e)
                failed.append({**company, "error": str(e)})
                continue

            entry = {**company, "company_years": company_years}
            if bucket == "ready":
                ready.append(entry)
                total_company_years += company_years
            elif bucket == "no_usable_documents":
                no_usable.append(entry)
            else:
                no_documents.append(entry)

    return {
        "ready_count": len(ready),
        "company_years_to_run": total_company_years,
        "model_calls_to_run": total_company_years * 2,
        "no_usable_documents_count": len(no_usable),
        "no_usable_documents": no_usable[:50],
        "no_documents_count": len(no_documents),
        "no_documents": no_documents[:50],
        "check_failed_count": len(failed),
        "check_failed": failed[:50],
    }


def _cohort_from_inline(entries: List[Union[int, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Turn the in-code REN_COMPANIES list (ids and/or names) into cohort rows."""
    rows: List[Dict[str, Any]] = []
    direct: List[Dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            direct.append({"company_id": entry, "company_name": None,
                           "ticker": None, "exchange": None})
        elif str(entry).strip():
            rows.append({"company_name": str(entry).strip()})
    return rows, direct


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_ren_for_all_companies(request):
    """
    POST /ren/extract/all/

    Runs the CITIES pipeline over a company cohort, batching companies and
    dispatching a Celery group per batch on the `ren` queue.

    POST body (all optional):
    {
      "s3_key": "cohorts/DGI FINAL LIST 07 08 2026.xlsx",  // cohort spreadsheet
      "company_ids": [1,2,3],     // canonical ids, skips resolution entirely
      "companies": ["Acme plc"],  // override REN_COMPANIES for this run
      "listed_only": false,       // drop rows whose Listed column isn't Yes
      "batch_size": 40,
      "year": 2024,               // single reporting year
      "years": 2,                 // how many recent years, default 2
      "dry_run": false,           // resolve and report, dispatch nothing
      "check_documents": false    // with dry_run: also count usable company-years
    }

    dry_run is worth using first on a large cohort: it reports how many rows
    parsed, how many de-duplicated, and how many resolved, without spending a
    single model call. Adding check_documents also inspects each company's
    documents, which is how a resolution mismatch shows up — every ReN company
    should already have ingested documents, so anything with none matched the
    wrong canonical company.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        batch_size = int(request.data.get("batch_size", DEFAULT_BATCH_SIZE))
        if batch_size <= 0:
            batch_size = DEFAULT_BATCH_SIZE
    except Exception:
        batch_size = DEFAULT_BATCH_SIZE

    year = request.data.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except Exception:
        return Response({"error": "Invalid 'year' value"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        years = int(request.data.get("years", DEFAULT_YEARS))
        if years <= 0:
            years = DEFAULT_YEARS
    except Exception:
        years = DEFAULT_YEARS

    dry_run = str(request.data.get("dry_run", False)).lower() in ("1", "true", "yes")
    listed_only = str(request.data.get("listed_only", False)).lower() in ("1", "true", "yes")
    # Off by default: it costs one DataX read per company, so on a full cohort
    # it takes a while. Worth it before a real run.
    check_docs = str(request.data.get("check_documents", False)).lower() in ("1", "true", "yes")

    explicit_ids = request.data.get("company_ids")
    s3_key = request.data.get("s3_key")
    override = request.data.get("companies")

    skipped: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    source = None

    # ---- 1. explicit ids ----
    if isinstance(explicit_ids, list) and explicit_ids:
        try:
            resolved = [{"company_id": int(c), "company_name": None,
                         "ticker": None, "exchange": None} for c in explicit_ids]
        except Exception:
            return Response({"error": "company_ids must be a list of integers"},
                            status=status.HTTP_400_BAD_REQUEST)
        source = "company_ids"

    # ---- 2. cohort spreadsheet in GCS ----
    elif s3_key:
        os.makedirs("pdfs", exist_ok=True)
        local_path = os.path.join("pdfs", os.path.basename(s3_key))
        ok = download_file_from_gcs(BUCKET_NAME, s3_key, download_path=local_path)
        if not ok or not os.path.exists(local_path):
            return Response({"error": f"Failed to download or locate {s3_key}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        try:
            companies, skipped = load_cohort(local_path, listed_only=listed_only)
        except Exception as e:
            logger.error("[ren] Failed to parse cohort %s: %s", s3_key, e)
            return Response({"error": f"Failed to parse cohort sheet: {e}"},
                            status=status.HTTP_400_BAD_REQUEST)
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

        resolved, unresolved = resolve_cohort(companies, uid)
        source = f"s3_key:{s3_key}"

    # ---- 3. the in-code list ----
    else:
        cohort = override if isinstance(override, list) and override else REN_COMPANIES
        if not cohort:
            return Response(
                {
                    "message": "No cohort supplied — pass 's3_key' with the cohort "
                               "spreadsheet, 'company_ids', or populate "
                               "REN_COMPANIES in tasks/ren/companies.py.",
                    "total_companies": 0,
                },
                status=status.HTTP_200_OK,
            )
        rows, direct = _cohort_from_inline(cohort)
        resolved, unresolved = resolve_cohort(rows, uid)
        resolved.extend(direct)
        source = "REN_COMPANIES" if cohort is REN_COMPANIES else "companies"

    if not resolved:
        return Response(
            {
                "message": "No companies could be resolved.",
                "source": source,
                "total_companies": 0,
                "skipped": skipped,
                "unresolved": unresolved,
            },
            status=status.HTTP_200_OK,
        )

    summary: Dict[str, Any] = {
        "source": source,
        "total_companies": len(resolved),
        "skipped_rows": len(skipped),
        "skipped": skipped[:50],
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:50],
        "filtered_by_year": year,
        "years": years,
    }

    if dry_run:
        summary["message"] = (
            "Dry run — nothing dispatched. Each company runs up to "
            f"{years} company-years, so at most {len(resolved) * years * 2} "
            "model calls.")
        summary["company_ids"] = [c["company_id"] for c in resolved]

        if check_docs:
            summary["documents"] = check_documents(resolved, uid, years, year)
            summary["message"] = (
                "Dry run — nothing dispatched. "
                f"{summary['documents']['ready_count']} of {len(resolved)} "
                "companies have a usable Annual Report; "
                f"{summary['documents']['company_years_to_run']} company-years "
                f"= {summary['documents']['model_calls_to_run']} model calls. "
                "Companies under 'no_documents' resolved to a canonical id but "
                "have nothing ingested, which points at a resolution mismatch.")

        return Response(summary, status=status.HTTP_200_OK)

    batches = chunked(resolved, batch_size)

    batch_group_infos: List[Dict[str, Any]] = []
    for idx, company_batch in enumerate(batches):
        signatures = [
            extract_ren_for_company.s(
                c["company_id"], uid, year=year, years=years,
                company_name=c["company_name"])
            for c in company_batch
        ]
        async_res = group(signatures).apply_async()
        batch_group_infos.append(
            {
                "batch_index": idx,
                "group_id": async_res.id,
                "company_ids": [c["company_id"] for c in company_batch],
                "count": len(company_batch),
            }
        )

    summary.update({
        "message": "Scheduled ReN CITIES extraction for all batches.",
        "batch_size": batch_size,
        "num_batches": len(batches),
        "batches": batch_group_infos,
    })
    return Response(summary, status=status.HTTP_202_ACCEPTED)
