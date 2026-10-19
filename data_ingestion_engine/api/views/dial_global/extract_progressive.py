# data_ingestion_engine/api/views/dial_global/extract_progressive.py
#
# Fan-out for the PROGRESSIVE DIAL Global pipeline.
#
# The joint endpoint (/dial-global/extract/all/) reads a whole company-year in
# one model call and caps that call at six documents, so a curated set larger
# than six is silently truncated. This one runs the same engine in progressive
# mode: one call per REPORT TYPE, with the still-unanswered questions carried
# forward, so a 19-document company-year is read in full.
#
# Cohort sources, in order of precedence:
#   1. "company_ids"  — dial_global_companies ids, no resolution
#   2. every DIAL Global company, as the joint endpoint does
#
# Ids here index dial_global_companies, NOT the canonical `companies` table the
# ReN pipeline uses. The two id spaces overlap numerically, so a ReN id sent
# here would extract a different company entirely.

import logging
from typing import Any, Dict, List

from celery import group
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.dial_global.extract import (
    extract_dial_global_progressive_for_company,
)
from data_ingestion_engine.utils.dial_global.store_data import (
    fetch_dial_global_companies,
)

logger = logging.getLogger(__name__)

# Smaller than the joint endpoint's: a progressive task is several sequential
# model calls, so a batch holds its Celery slots for longer.
DEFAULT_BATCH_SIZE = 20

# Above this many companies, a run that was not given an explicit id list must
# echo the count back in `confirm`. Extraction REPLACES stored rows in place and
# DataX keeps no history, so "run everything" should not be reachable from a
# request body with no numbers in it.
CONFIRM_THRESHOLD = 25


def chunked(seq: List[Any], size: int) -> List[List[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_dial_global_progressive(request):
    """
    POST /dial-global/extract/progressive/

    POST body (all optional):
    {
      "company_ids": [1, 2, 3],   // dial_global_companies ids
      "year": 2026,               // single reporting year; default every year
      "document_types": ["DEI Report"],   // whitelist of stored types
      "batch_size": 20,
      "dry_run": false,
      "confirm": "40"             // required for a whole-universe run
    }

    ALWAYS dry_run first. A real run REPLACES the stored row for every
    company-year it touches.
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
        return Response({"error": "Invalid 'year' value"},
                        status=status.HTTP_400_BAD_REQUEST)

    document_types = request.data.get("document_types") or None
    if document_types is not None and not isinstance(document_types, list):
        return Response({"error": "document_types must be a list"},
                        status=status.HTTP_400_BAD_REQUEST)

    dry_run = str(request.data.get("dry_run", False)).lower() in ("1", "true", "yes")

    explicit = request.data.get("company_ids")
    if explicit:
        try:
            seen, company_ids = set(), []
            for value in explicit:
                company_id = int(value)
                if company_id not in seen:
                    seen.add(company_id)
                    company_ids.append(company_id)
        except (TypeError, ValueError):
            return Response({"error": "company_ids must be a list of integers"},
                            status=status.HTTP_400_BAD_REQUEST)
        source = "company_ids"
    else:
        companies = fetch_dial_global_companies(uid) or []
        company_ids = sorted({
            int(c["company_id"]) for c in companies
            if c.get("company_id") is not None})
        source = "all_dial_global_companies"

    if not company_ids:
        return Response({"message": "No companies to run.", "source": source,
                         "total_companies": 0}, status=status.HTTP_200_OK)

    summary: Dict[str, Any] = {
        "source": source,
        "mode": "progressive",
        "universe": "DG",
        "total_companies": len(company_ids),
        "year": year,
        "document_types": document_types,
    }

    if dry_run:
        summary["message"] = (
            f"Dry run — nothing dispatched. {len(company_ids)} company/companies"
            f"{f', year {year}' if year else ', every year they hold'}. Each "
            f"company-year costs one extraction call per REPORT TYPE plus one "
            f"scoring call. NOTE: a real run REPLACES the stored row for every "
            f"company-year it touches.")
        summary["company_ids"] = company_ids
        return Response(summary, status=status.HTTP_200_OK)

    if source != "company_ids" and len(company_ids) > CONFIRM_THRESHOLD:
        if str(request.data.get("confirm", "")).strip() != str(len(company_ids)):
            return Response(
                {
                    "error": "Confirmation required for a whole-universe run.",
                    "detail": (
                        f"This would dispatch {len(company_ids)} companies and "
                        f"REPLACE every stored company-year they hold."),
                    "how_to_proceed": {
                        "inspect_first": {"dry_run": True},
                        "pilot_a_few": {"company_ids": company_ids[:3]},
                        "really_run_all": {"confirm": str(len(company_ids))},
                    },
                    "total_companies": len(company_ids),
                },
                status=status.HTTP_400_BAD_REQUEST)

    batches = chunked(company_ids, batch_size)
    batch_group_infos: List[Dict[str, Any]] = []
    for index, batch in enumerate(batches):
        signatures = [
            extract_dial_global_progressive_for_company.s(
                company_id, uid, year=year, document_types=document_types)
            for company_id in batch
        ]
        async_res = group(signatures).apply_async()
        batch_group_infos.append({
            "batch_index": index,
            "group_id": async_res.id,
            "company_ids": batch,
            "count": len(batch),
        })

    summary.update({
        "message": "Scheduled progressive DIAL Global extraction.",
        "batch_size": batch_size,
        "num_batches": len(batches),
        "batches": batch_group_infos,
    })
    return Response(summary, status=status.HTTP_202_ACCEPTED)
