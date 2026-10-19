from typing import List
from celery import group
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status
from data_ingestion_engine.tasks.extract_env_gov import extract_env_gov_for_company
from data_ingestion_engine.utils.store_data import DATAX_INTERNAL, _headers, _session
from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated
from .extract_all_env_gov import list_all_company_ids


def chunk_slice(items: List[int], page: int, page_size: int) -> List[int]:
    if page < 1:
        page = 1
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end]


def list_company_ids_page(uid: str, page: int, page_size: int, with_docs: bool = True):
    url = (
        f"{DATAX_INTERNAL}/companies/ids"
        f"?with_docs={'true' if with_docs else 'false'}"
        f"&page={page}&page_size={page_size}"
    )
    with _session() as s:
        resp = s.get(url, headers=_headers(uid), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "items": data.get("items", []),
        "page": data.get("page", page),
        "page_size": data.get("page_size", page_size),
        "total": data.get("total", 0),
        "total_pages": data.get("total_pages", 0),
    }


def list_company_ids_missing_env(uid: str, page: int, page_size: int, with_docs: bool = True):
    """
    Returns companies that have docs in `reports` and NO record in `environmental_data`.
    Assumes DATAx supports `missing_env=true` as a filter.
    """
    url = (
        f"{DATAX_INTERNAL}/companies/ids"
        f"?with_docs={'true' if with_docs else 'false'}"
        f"&missing_env=true"
        f"&page={page}&page_size={page_size}"
    )
    with _session() as s:
        resp = s.get(url, headers=_headers(uid), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "items": data.get("items", []),
        "page": data.get("page", page),
        "page_size": data.get("page_size", page_size),
        "total": data.get("total", 0),
        "total_pages": data.get("total_pages", 0),
    }


def list_company_ids_with_docs_for_year(uid: str, year: int, page: int, page_size: int):
    """
    Companies that have at least one report for the given year.
    Assumes DATAx supports year filters. We try a couple of param names defensively.
    """
    # Prefer explicit ?year=YYYY; also try ?reports_year=YYYY for older servers.
    base = f"{DATAX_INTERNAL}/companies/ids?with_docs=true&page={page}&page_size={page_size}"
    url = f"{base}&year={year}"
    with _session() as s:
        resp = s.get(url, headers=_headers(uid), timeout=30)
    if resp.status_code == 400:  # fallback to reports_year
        url = f"{base}&reports_year={year}"
        with _session() as s:
            resp = s.get(url, headers=_headers(uid), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "items": data.get("items", []),
        "page": data.get("page", page),
        "page_size": data.get("page_size", page_size),
        "total": data.get("total", 0),
        "total_pages": data.get("total_pages", 0),
    }


def list_company_ids_missing_env_for_year(uid: str, year: int, page: int, page_size: int):
    """
    Companies that HAVE a report in `reports` for {year} and do NOT have an entry
    in `environmental_data` for {year}.
    Assumes DATAx supports a year-scoped missing filter.
    """
    base = f"{DATAX_INTERNAL}/companies/ids?with_docs=true&page={page}&page_size={page_size}"
    # First try the explicit year-scoped flag
    url = f"{base}&year={year}"
    with _session() as s:
        resp = s.get(url, headers=_headers(uid), timeout=30)
    if resp.status_code == 400:
        # Fallback to generic missing_env + year filter combo
        url = f"{base}&missing_env=true&year={year}"
        with _session() as s:
            resp = s.get(url, headers=_headers(uid), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "items": data.get("items", []),
        "page": data.get("page", page),
        "page_size": data.get("page_size", page_size),
        "total": data.get("total", 0),
        "total_pages": data.get("total_pages", 0),
    }


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_env_gov_page(request):
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    page = int(request.data.get("page", 1))
    page_size = int(request.data.get("page_size", 50))
    explicit_ids = request.data.get("company_ids")

    # Optional year to target (e.g., 2025). If provided, we scope all queries to that year.
    year = request.data.get("year")
    target_year = int(year) if year not in (None, "",) else None

    # Default behavior unchanged, but if 'year' is provided we prefer "missing for that year"
    only_missing = str(request.data.get("only_missing", "true")
                       ).lower() in ("1", "true", "yes")

    if isinstance(explicit_ids, list) and explicit_ids:
        company_ids = sorted({int(c) for c in explicit_ids})
        total = len(company_ids)
        total_pages = 1
        current_page = 1
        source = "explicit_ids"
    else:
        if target_year is not None:
            if only_missing:
                payload = list_company_ids_missing_env_for_year(
                    uid=uid, year=target_year, page=page, page_size=page_size
                )
                source = f"with_docs_and_missing_environmental_data_{target_year}"
            else:
                payload = list_company_ids_with_docs_for_year(
                    uid=uid, year=target_year, page=page, page_size=page_size
                )
                source = f"with_docs_any_{target_year}"
        else:
            if only_missing:
                payload = list_company_ids_missing_env(
                    uid=uid, page=page, page_size=page_size, with_docs=True
                )
                source = "with_docs_and_missing_environmental_data"
            else:
                payload = list_company_ids_page(
                    uid=uid, page=page, page_size=page_size, with_docs=True
                )
                source = "with_docs_any"

        company_ids = sorted({int(c) for c in payload["items"]})
        total = payload["total"]
        total_pages = payload["total_pages"]
        current_page = payload["page"]

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

    if not company_ids:
        return Response(
            {
                "message": "No companies found for requested page.",
                "page": current_page,
                "page_size": page_size,
                "filter": "missing_env" if only_missing else "with_docs",
                "year": target_year,
            },
            status=status.HTTP_200_OK,
        )

    # Kick off Celery group; pass target_year as a kwarg (backward compatible if task ignores it)
    signatures = [
        extract_env_gov_for_company.s(cid, uid, target_year=target_year) for cid in company_ids
    ]
    async_res = group(signatures).apply_async()

    return Response(
        {
            "message": "Scheduled Environmental & Governance extraction for page.",
            "page": current_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_companies": total,
            "count": len(company_ids),
            "company_ids": company_ids,
            "group_id": async_res.id,
            "source": source,
            "filter": "missing_env" if only_missing else "with_docs",
            "year": target_year,
        },
        status=status.HTTP_202_ACCEPTED,
    )
