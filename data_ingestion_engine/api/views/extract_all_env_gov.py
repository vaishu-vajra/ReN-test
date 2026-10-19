
# api/views/extract_all_env_gov.py
from typing import List, Dict, Any, Optional
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status

from celery import group
from rest_framework.permissions import IsAuthenticated

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.extract_env_gov import extract_env_gov_for_company
from data_ingestion_engine.utils.store_data import DATAX_INTERNAL, _headers, _session

# Your HTTP session + headers helper


def chunked(seq: List[int], size: int) -> List[List[int]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def list_all_company_ids(uid: str, page_size: int = 50) -> List[int]:
    all_ids: List[int] = []
    page = 1
    while True:
        url = f"{DATAX_INTERNAL}/companies/ids?with_docs=true&page={page}&page_size={page_size}"
        with _session() as s:
            resp = s.get(url, headers=_headers(uid), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_ids.extend(data.get("items", []))
        if page >= data.get("total_pages", 0):
            break
        page += 1
    return all_ids


def chunked(seq: List[int], size: int) -> List[List[int]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def get_company_ids_missing_env(uid: str, year: Optional[int] = None) -> List[int]:
    """
    Calls DataX internal: GET /companies/ids/all[?year=YYYY]
    Returns all company IDs that HAVE reports (of allowed types)
    but NO rows in environmental_data (optionally for that year).
    """
    if not uid:
        return []
    url = f"{DATAX_INTERNAL}/companies/ids/all"
    if year is not None:
        url = f"{url}?year={int(year)}"

    with _session() as s:
        resp = s.get(url, headers=_headers(uid), timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Defensive: ensure list of ints
    if not isinstance(data, list):
        raise ValueError(
            "Unexpected response from /companies/ids/all: expected a JSON list")
    try:
        return [int(x) for x in data]
    except Exception as e:
        raise ValueError(
            f"Invalid id in /companies/ids/all response: {e}") from e


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_env_gov_for_all_companies(request):
    """
    Batches companies (default 40 per batch) that HAVE reports but NO env data
    (optionally restricted by year), and triggers Celery groups to extract
    Environmental & Governance data.

    POST body (optional):
    {
      "batch_size": 40,
      "company_ids": [1,2,3],   // optional explicit override
      "year": 2023              // optional; filters both report/env checks
    }
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # batch_size
    try:
        batch_size = int(request.data.get("batch_size", 40))
        if batch_size <= 0:
            batch_size = 40
    except Exception:
        batch_size = 40

    # optional year
    year = request.data.get("year")
    try:
        year = int(year) if year is not None else None
    except Exception:
        return Response({"error": "Invalid 'year' value"}, status=status.HTTP_400_BAD_REQUEST)

    # Company IDs: explicit override or fetch from /companies/ids/all
    explicit_ids = request.data.get("company_ids")
    if isinstance(explicit_ids, list) and explicit_ids:
        try:
            company_ids = [int(x) for x in explicit_ids]
        except Exception:
            return Response({"error": "company_ids must be a list of integers"}, status=status.HTTP_400_BAD_REQUEST)
    else:
        try:
            company_ids = get_company_ids_missing_env(uid, year=year)
        except Exception as e:
            return Response({"error": f"Failed to fetch IDs: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

    if not company_ids:
        return Response(
            {
                "message": "No companies needing Environmental/Governance extraction were found.",
                "total_companies": 0,
                "filtered_by_year": year,
            },
            status=status.HTTP_200_OK,
        )

    batches = chunked(company_ids, batch_size)

    batch_group_infos: List[Dict[str, Any]] = []
    for idx, company_batch in enumerate(batches):
        signatures = [extract_env_gov_for_company.s(
            cid, uid, year=year) for cid in company_batch]
        grp = group(signatures)
        async_res = grp.apply_async()
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
            "message": "Scheduled Environmental & Governance extraction for all batches.",
            "total_companies": len(company_ids),
            "filtered_by_year": year,
            "batch_size": batch_size,
            "num_batches": len(batches),
            "batches": batch_group_infos,
        },
        status=status.HTTP_202_ACCEPTED,
    )
