# data_ingestion_engine/api/views/dial_global/extract_all.py
#
# Fan-out for the DIAL Global extraction engine. Mirrors
# api/views/extract_all_env_gov.py — same batching, same Celery group per
# batch, same 202 response shape — but over dial_global_companies.

import logging
from typing import Any, Dict, List

from celery import group
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.dial_global.extract import extract_dial_global_for_company
from data_ingestion_engine.utils.dial_global.store_data import list_dial_global_company_ids

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 40


def chunked(seq: List[int], size: int) -> List[List[int]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_dial_global_for_all_companies(request):
    """
    POST /dial-global/extract/all/

    Batches every company in the DIAL Global tables (40 per batch by default)
    and dispatches a Celery group per batch on the `dial_global` queue.

    POST body (all optional):
    {
      "batch_size": 40,
      "company_ids": [1,2,3],            // explicit override
      "year": 2024,                      // restrict to one reporting year
      "document_types": ["Annual Report"] // restrict to certain document types
    }
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

    document_types = request.data.get("document_types")
    if document_types is not None and not isinstance(document_types, list):
        return Response(
            {"error": "document_types must be a list of strings"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    explicit_ids = request.data.get("company_ids")
    if isinstance(explicit_ids, list) and explicit_ids:
        try:
            company_ids = [int(x) for x in explicit_ids]
        except Exception:
            return Response(
                {"error": "company_ids must be a list of integers"},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        try:
            company_ids = list_dial_global_company_ids(uid)
        except Exception as e:
            logger.error("[dial_global] Failed to list company IDs: %s", e)
            return Response(
                {"error": f"Failed to fetch DIAL Global company IDs: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

    if not company_ids:
        return Response(
            {
                "message": "No DIAL Global companies were found.",
                "total_companies": 0,
                "filtered_by_year": year,
            },
            status=status.HTTP_200_OK,
        )

    batches = chunked(company_ids, batch_size)

    batch_group_infos: List[Dict[str, Any]] = []
    for idx, company_batch in enumerate(batches):
        signatures = [
            extract_dial_global_for_company.s(
                cid, uid, year=year, document_types=document_types)
            for cid in company_batch
        ]
        async_res = group(signatures).apply_async()
        batch_group_infos.append(
            {
                "batch_index": idx,
                "group_id": async_res.id,
                "company_ids": company_batch,
                "count": len(company_batch),
            }
        )

    return Response(
        {
            "message": "Scheduled DIAL Global extraction for all batches.",
            "total_companies": len(company_ids),
            "filtered_by_year": year,
            "document_types": document_types,
            "batch_size": batch_size,
            "num_batches": len(batches),
            "batches": batch_group_infos,
        },
        status=status.HTTP_202_ACCEPTED,
    )
