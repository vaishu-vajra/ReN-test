# data_ingestion_engine/api/views/dial_global/extract_paginated.py
#
# Single-page variant of the DIAL Global fan-out, mirroring
# api/views/extract_env_gov_paginated.py.
#
# Paging is done client-side: DataX's GET /internal/dial-global/companies/
# returns the whole table with no page parameters (see
# utils/dial_global/store_data.py). The response still reports page /
# total_pages so the caller drives it the same way as the env/gov endpoint.

import logging

from celery import group
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.dial_global.extract import extract_dial_global_for_company
from data_ingestion_engine.utils.dial_global.store_data import list_dial_global_company_ids

logger = logging.getLogger(__name__)


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_dial_global_page(request):
    """
    POST /dial-global/extract/page/

    POST body (all optional):
    {
      "page": 1,
      "page_size": 50,
      "company_ids": [1,2,3],             // explicit override, ignores paging
      "year": 2024,
      "document_types": ["Annual Report"]
    }
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        page = int(request.data.get("page", 1))
        page_size = int(request.data.get("page_size", 50))
    except Exception:
        return Response({"error": "Invalid 'page' or 'page_size'"}, status=status.HTTP_400_BAD_REQUEST)
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50

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
            company_ids = sorted({int(c) for c in explicit_ids})
        except Exception:
            return Response(
                {"error": "company_ids must be a list of integers"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        total = len(company_ids)
        total_pages = 1
        current_page = 1
        source = "explicit_ids"
    else:
        try:
            all_ids = list_dial_global_company_ids(uid)
        except Exception as e:
            logger.error("[dial_global] Failed to list company IDs: %s", e)
            return Response(
                {"error": f"Failed to fetch DIAL Global company IDs: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        total = len(all_ids)
        total_pages = (total + page_size - 1) // page_size
        current_page = page
        source = "dial_global_companies"

        if total_pages and current_page > total_pages:
            return Response(
                {
                    "message": "Page out of range",
                    "page": current_page,
                    "total_pages": total_pages,
                    "total_companies": total,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start = (current_page - 1) * page_size
        company_ids = all_ids[start:start + page_size]

    if not company_ids:
        return Response(
            {
                "message": "No DIAL Global companies found for requested page.",
                "page": current_page,
                "page_size": page_size,
                "year": year,
            },
            status=status.HTTP_200_OK,
        )

    signatures = [
        extract_dial_global_for_company.s(
            cid, uid, year=year, document_types=document_types)
        for cid in company_ids
    ]
    async_res = group(signatures).apply_async()

    return Response(
        {
            "message": "Scheduled DIAL Global extraction for page.",
            "page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_companies": total,
            "count": len(company_ids),
            "company_ids": company_ids,
            "group_id": async_res.id,
            "source": source,
            "year": year,
            "document_types": document_types,
        },
        status=status.HTTP_202_ACCEPTED,
    )
