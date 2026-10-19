from datetime import date, datetime
from decimal import Decimal
import json
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import os
import requests
import logging
from dotenv import load_dotenv

from internal_jwt import mint_internal_jwt
from data_ingestion_engine.utils.handle_metadata_value import handle_metadata_value


def _safe_year(value: Any, default: int = 1900) -> int:
    """Coerce a year to int, tolerating NaN / None / '' / '2024.0' etc."""
    try:
        text = handle_metadata_value(value)
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


load_dotenv()
logger = logging.getLogger(__name__)

DATAX_API_URL = (os.getenv("DATAX_API_URL") or "").rstrip("/")
if not DATAX_API_URL:
    raise RuntimeError("DATAX_API_URL is not set")

DATAX_INTERNAL = f"{DATAX_API_URL}/internal"

# ---------- HTTP helpers ----------


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _headers(uid: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {mint_internal_jwt(uid)}",
        "Content-Type": "application/json",
    }

# ---------- Core calls (INTERNAL) ----------


def store_data(section: str, data: Any, year: int, company_id: int, uid: str) -> bool:
    """
    Stores processed data through DATAx INTERNAL API endpoints.
    Uses internal JWT instead of Firebase token.
    """
    if data is None:
        print(f"No data provided for {section}")
        return False

    # Convert model → dict once
    if isinstance(data, BaseModel):
        data = data.model_dump()

    print("Storing data...")
    print(section + "_data", data)

    if not data:
        print(f"No data to store for {section}")
        return False

    endpoints: List[Dict[str, Any]] = []

    try:
        if section == "overview and key-metrics":
            data = data.get("highlights", data) if isinstance(
                data, dict) else data
            # class HighlightsSchema(BaseModel):
            #     """A summary of the company's DEI initiatives and performance."""
            #     company_dei_overview: Optional[str] = Field(
            #         None, description="Overview of the company's DEI initiatives and performance.")
            #     key_metrics_summary: Optional[str] = Field(
            #         None, description="A brief summary or narrative of the company's key DEI metrics.")
            #     notable_achievements: Optional[List[str]] = Field(
            #         None, description="A list of notable achievements or milestones in the area of DEI.")

            # what i expect in data to send in endpoints:
            #         class HighlightBase(BaseModel):
            # company_id: int = Field(..., description="Unique ID of the company")
            # company_dei_overview: Optional[str] = Field(None, description="DEI Overview of the company")
            # key_metrics_summary: Optional[List[Optional[str]]] = Field(
            #     default_factory=list,
            #     description="Summary of key metrics"
            # )
            # year: int = Field(..., description="Year of the highlight")
            # quarter: Optional[str] = Field(None, description="Quarter of the highlight (e.g., Q1, Q2)")

            # @field_validator('key_metrics_summary', mode='before')
            # def remove_none_from_key_metrics_summary(cls, v):
            #     if v is None:
            #         return []
            #     # Filter out None values
            #     return [item for item in v if item is not None]

            # model_config = {
            #     "from_attributes": True
            # }

            overview = data.get("company_dei_overview")
            key_metrics = _normalize_list_of_strings(
                data.get("key_metrics_summary"))
            notable_achievements = data.get("notable_achievements", [])

            highlights_payload = {
                "company_id": company_id,
                "year": year,
                "quarter": "Q4",
                "company_dei_overview": overview,
                "key_metrics_summary": key_metrics,  # ALWAYS list[str]
            }
            endpoints.append({
                "url": f"{DATAX_INTERNAL}/highlights/",
                "data": highlights_payload,
                "name": "highlights",
            })
            # what i expect in data to send in endpoints:
            # class NotableAchievementBase(BaseModel):
            # company_id: int = Field(..., description="Unique ID of the company")
            # description: str = Field(..., description="Description of the achievement")
            # year: int = Field(..., description="Year of the achievement")
            # quarter: str = Field(..., description="Quarter of the achievement (e.g., Q1, Q2)")

            # @field_validator('year')
            # def validate_year(cls, v):
            #     current_year = datetime.now().year
            #     if v < 1900 or v > current_year + 100:
            #         raise ValueError('Year must be between 1900 and 100 next years.')
            #     return v

            # @field_validator('quarter')
            # def validate_quarter(cls, v):
            #     if v not in ("Q1", "Q2", "Q3", "Q4"):
            #         raise ValueError('Quarter must be one of Q1, Q2, Q3, Q4.')
            #     return v

            # # Configure model to allow instantiation from ORM objects
            # model_config = {
            #     "from_attributes": True
            # }

            for ach in (notable_achievements or []):
                endpoints.append({
                    "url": f"{DATAX_INTERNAL}/notable-achievements/",
                    "data": {
                        "company_id": company_id,
                        "year": year,
                        "quarter": "Q4",
                        "description": ach,
                    },
                    "name": "notable-achievements",
                })

        elif section == "key-personal":
            key_personnel = data.get("key_personnel", [])
            if not key_personnel:
                print("No key personnel data found")
                return False

            for key_person in key_personnel:
                key_person["company_id"] = company_id
                key_person["year"] = year
                key_person["quarter"] = "Q4"
                key_person["exists"] = True

                endpoints.append({
                    "url": f"{DATAX_INTERNAL}/key-personnel/",
                    "data": key_person,
                    "name": "key-personnel"
                })

        elif section == "key-priorities":
            dei_priorities = data.get("dei_priorities") or {}
            strategic_priorities = dei_priorities.get(
                "strategic_priorities", [])
            aspirational_goals = dei_priorities.get("aspirational_goals", [])
            if not strategic_priorities and not aspirational_goals:
                print(
                    "No strategic priorities or aspirational goals to store, skipping.")
                return False

            # Aspirational goals
            if aspirational_goals and isinstance(aspirational_goals, list):
                for goal in aspirational_goals:
                    refined_goal = refine_aspirational_goal(
                        goal, company_id, year, "Q4")
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/aspirational-goals/",
                        "data": refined_goal,
                        "name": "aspirational-goals"
                    })

            # Strategic priorities
            if strategic_priorities and isinstance(strategic_priorities, list):
                for priority in strategic_priorities:
                    refined_priority = refine_strategic_priority(
                        priority, company_id, year, "Q4")
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/strategic-priorities/{company_id}/",
                        "data": refined_priority,
                        "name": "strategic-priorities"
                    })

        elif section == "key-initiatives":
            dei_programs = data.get("dei_programs", [])
            if not dei_programs:
                print("No DEI programs found")
                return False

            if not isinstance(dei_programs, list) and isinstance(dei_programs, dict) and "items" in dei_programs:
                dei_programs = dei_programs.get("items", [])

            for key_initiative in dei_programs:
                program_data = key_initiative.copy()

                if "budget" in program_data and isinstance(program_data["budget"], dict):
                    budget = program_data.pop("budget", {})
                    program_data["budget_amount"] = budget.get("amount")
                    program_data["budget_currency"] = budget.get("currency")
                    program_data["budget_period"] = budget.get("period")

                if "success_metrics" in program_data:
                    program_data.pop("success_metrics", None)

                for k, v in list(program_data.items()):
                    if v == "None":
                        program_data[k] = None

                program_data["company_id"] = company_id
                program_data["year"] = year
                program_data["quarter"] = "Q4"

                endpoints.append({
                    "url": f"{DATAX_INTERNAL}/dei-programs/{company_id}/",
                    "data": program_data,
                    "name": "dei-programs"
                })

        elif section == "board-members":
            if not isinstance(data, dict):
                print("Invalid board members data")
                return False

            governance = data.get("dei_governance", {})
            leadership = governance.get("leadership", {})
            board_info = leadership.get("board_of_directors", {})

            if not board_info:
                print("No board information found")
                return False

            board_exists = board_info.get("exists", False)
            board_members = board_info.get("members", [])

            if board_exists and board_members and isinstance(board_members, list):
                for member in board_members:
                    member["company_id"] = company_id
                    member["year"] = year
                    member["quarter"] = "Q4"
                    member.setdefault("committee_memberships", [])
                    member.setdefault("expertise", [])
                    member.setdefault("biography", "")
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/board-members/",
                        "data": member,
                        "name": "board-members"
                    })

        elif section == "dimensions-summary":
            dimensions = data.get("dimensions", [])
            if not dimensions:
                print("No dimensions data found")
                return False

            for dim in dimensions:
                if "gender_summary" in dim:
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Gender Diversity",
                            "metric_category": "Summary",
                            "metric_name": "Gender Summary",
                            "description": dim.get("gender_summary"),
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })
                if "ethnicity_summary" in dim:
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Ethnicity Diversity",
                            "metric_category": "Summary",
                            "metric_name": "Ethnicity Summary",
                            "description": dim.get("ethnicity_summary"),
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })
                if "other_dimensions_summary" in dim:
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Other Diversity",
                            "metric_category": "Summary",
                            "metric_name": "Other Dimensions Summary",
                            "description": dim.get("other_dimensions_summary"),
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })

                for quote in (dim.get("gender_quotes", []) or []):
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Gender Diversity",
                            "metric_category": "Quote",
                            "metric_name": "Gender Dimensions Quotes",
                            "description": quote,
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })

                for quote in (dim.get("ethnicity_quotes", []) or []):
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Ethnicity Diversity",
                            "metric_category": "Quote",
                            "metric_name": "Ethnicity Dimensions Quotes",
                            "description": quote,
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })

                for quote in (dim.get("other_dimensions_quotes", []) or []):
                    endpoints.append({
                        "url": f"{DATAX_INTERNAL}/metrics/",
                        "data": {
                            "company_id": company_id,
                            "dimension_name": "Other Diversity",
                            "metric_category": "Quote",
                            "metric_name": "Other Dimensions Quotes",
                            "description": quote,
                            "year": year,
                            "quarter": "Q4"
                        },
                        "name": "dimensions"
                    })

                target_mappings = {
                    "Gender Diversity": [
                        ("board_gender_target", "board_gender_target_year",
                         "Board", "Board gender target %"),
                        ("executive_gender_target", "executive_gender_target_year",
                         "Executives", "Executive gender target %"),
                        ("all_employees_gender_target", "all_employees_gender_target_year",
                         "All Employees", "All Employees gender target %"),
                    ],
                    "Ethnicity Diversity": [
                        ("board_ethnicity_target", "board_ethnicity_target_year",
                         "Board", "Board ethnicity target %"),
                        ("executive_ethnicity_target", "executive_ethnicity_target_year",
                         "Executives", "Executive ethnicity target %"),
                        ("all_employees_ethnicity_target", "all_employees_ethnicity_target_year",
                         "All Employees", "All Employees ethnicity target %"),
                    ],
                }

                for dimension_name, target_list in target_mappings.items():
                    for (value_key, period_key, metric_category, metric_name) in target_list:
                        target_value = dim.get(value_key)
                        target_period = dim.get(period_key)
                        if target_value not in (None, "None", ""):
                            endpoints.append({
                                "url": f"{DATAX_INTERNAL}/metrics/",
                                "data": {
                                    "company_id": company_id,
                                    "dimension_name": dimension_name,
                                    "metric_category": metric_category,
                                    "metric_name": metric_name,
                                    "value": target_value,
                                    "unit": "percent",
                                    "period": str(target_period) if target_period not in (None, "None", "") else None,
                                    "year": year,
                                    "quarter": "Q4",
                                },
                                "name": "dimensions"
                            })

        elif section == "environmental":
            environmental_data = data
            if not environmental_data:
                print("No environmental data found")
                return False
            endpoints.append({
                "url": f"{DATAX_INTERNAL}/environmental-data/",
                "data": {
                    "company_id": company_id,
                    "year": year,
                    "data": environmental_data,
                    "quarter": "Q4",
                },
                "name": "environmental-data"
            })

        elif section == "governance":
            governance_data = data
            if not governance_data:
                print("No governance data found")
                return False
            endpoints.append({
                "url": f"{DATAX_INTERNAL}/governance-data/",
                "data": {
                    "company_id": company_id,
                    "year": year,
                    "data": governance_data,
                    "quarter": "Q4",
                },
                "name": "governance-data"
            })
        elif section == "social":
            social_data = data
            if not social_data:
                print("No social data found")
                return False
            endpoints.append({
                "url": f"{DATAX_INTERNAL}/social-data/{company_id}",
                "data": {
                    "company_id": company_id,
                    "year": year,
                    "data": social_data,
                    "quarter": "Q4",
                },
                "name": "social-data"
            })
        # POST all prepared endpoints
        print("Endpoints:", endpoints)
        with _session() as s:
            for endpoint in endpoints:
                try:
                    safe_payload = _make_json_safe(
                        endpoint["data"])   # <<< add this line
                    resp = s.post(
                        endpoint["url"],
                        json=safe_payload,                              # <<< use safe payload
                        headers=_headers(uid),
                        timeout=60
                    )
                    if not resp.ok:
                        print(
                            f"Failed to store {endpoint['name']}. Status: {resp.status_code} Error: {resp.text}")
                        continue
                    print(
                        f"Stored {endpoint['name']}. Status: {resp.status_code}")
                except Exception as e:
                    print(f"Error storing {endpoint['name']}: {str(e)}")
                    continue
        return company_id

    except Exception as e:
        print(f"Error processing {section} data: {str(e)}")
        return False


def get_company_id(
    metadata: Dict[str, Any],
    uid: str,
    ticker: Optional[str] = None,
    exchange: Optional[str] = None,
    tickers: Optional[List[str]] = None,
    create: bool = True,
) -> Optional[int]:
    """
    Get or create company (upsert) via INTERNAL endpoint.
    Returns company_id or None.

    `create=False` runs DataX's identical matching logic but never writes,
    returning None when nothing matched. Use it from anything that only needs to
    LOOK UP an existing company — see resolve_company_id(). Ingestion, which is
    genuinely allowed to introduce a company, keeps the default.

    The T:EXCH ticker convention has been dropped:
    - `ticker_str` now carries plain, comma-separated symbols (dots kept), e.g.
      "ADM.L" or "BREE, BREE.L".
    - `exchange` is sent as its own field so DataX can disambiguate companies that
      share a ticker across exchanges.
    - `sector`, `industry`, and `country` are forwarded so DataX can persist them
      on the canonical `companies` row.

    When called with only (metadata, uid) — e.g. from the extraction task — the
    values are read from the metadata row instead.
    """

    def _clean(v: Any) -> str:
        return handle_metadata_value(v).strip() if v is not None else ""

    try:
        # --- Build plain ticker_str (no exchange encoding) ---
        if tickers:
            ticker_str = ", ".join(t.strip() for t in tickers if t and str(t).strip())
        else:
            ticker_str = _clean(metadata.get("Ticker Symbol", ""))

        exchange_str = _clean(exchange if exchange is not None else metadata.get("Exchange"))
        sector_str = _clean(metadata.get("Sector"))
        industry_str = _clean(metadata.get("Industry"))
        country_str = _clean(metadata.get("Country"))

        isin_str = metadata.get("ISIN", "")
        isin_list: List[str] = []
        if isin_str:
            for raw_isin in str(isin_str).split(","):
                i = raw_isin.strip()
                if i and i.upper() != "N/A":
                    isin_list.append(i)

        payload = {
            "company_name": metadata.get("Company Name", ""),
            "ticker_str": ticker_str,
            "exchange": exchange_str or None,
            "isins": isin_list,
            "sector": sector_str or None,
            "industry": industry_str or None,
            "country": country_str or None,
            "source_name": "Internal",
            "trigram_threshold": 0.8,
        }
        if not create:
            payload["create"] = False

        url = f"{DATAX_INTERNAL}/company-upsert/"
        with _session() as s:
            response = s.post(url, json=payload,
                              headers=_headers(uid), timeout=30)

        if response.status_code == 200:
            # With create=False DataX answers {"company_id": id|null, "matched": bool}.
            return response.json().get("company_id")

        print(
            f"Company {'upsert' if create else 'resolve'} failed: "
            f"{response.status_code} - {response.text}"
        )
        return None

    except Exception as e:
        print(f"Error in company {'upsert' if create else 'resolve'}: {str(e)}")
        return None


def resolve_company_id(
    metadata: Dict[str, Any],
    uid: str,
    **kwargs: Any,
) -> Optional[int]:
    """
    Look up an existing company. Never creates one.

    The extraction pipelines resolve a cohort row to a canonical id so they can
    read that company's documents. They must never introduce a company: a miss
    means the cohort row has no counterpart to extract, and creating an empty
    row there produces a company with no documents that scores nothing — and, if
    matching is imperfect, a duplicate of a company that already exists.

    Returns the company_id, or None when nothing matched.
    """
    return get_company_id(metadata, uid, create=False, **kwargs)


def fetch_company(company_id: int, uid: str) -> Optional[Dict[str, Any]]:
    """
    Read one canonical company row: name, symbol, exchange, isin, sector, and
    the has_reports/board/env flags.

    This is what makes a resolution auditable — it is the only way to see what
    identifiers a company actually holds, and so why a cohort row did or did not
    match it.
    """
    try:
        url = f"{DATAX_INTERNAL}/companies/{company_id}"
        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=30)

        if response.status_code == 200:
            return response.json()
        if response.status_code != 404:
            print(f"Fetching company {company_id} failed: "
                  f"{response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in fetch_company: {e}")
        return None


def fetch_company_brief(company_id: int, uid: str,
                        timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """
    The same company row as fetch_company(), on a short leash.

    fetch_company() runs on the shared retrying session: four attempts at 30s
    apiece, and a connection that stalls without ever failing can hold it far
    longer than that. For a batch job that is the right trade. On a request path
    it is fatal — the analysis endpoints need the company's name and exchange to
    build citation links, and they need it *before* the first byte of a streamed
    response, so a stall here reads to the caller as the endpoint hanging and to
    the operator as a worker timeout with no log line.

    Citations are decoration on an analysis that is worth streaming regardless,
    so this takes one attempt off the plain requests API (no retry adapter),
    caps it, and returns None on anything else. The caller degrades to a plain
    page reference.
    """
    try:
        response = requests.get(
            f"{DATAX_INTERNAL}/companies/{company_id}",
            headers=_headers(uid), timeout=timeout)
        if response.status_code == 200:
            return response.json()
        logger.warning("Company %s for citations: HTTP %s",
                       company_id, response.status_code)
    except Exception as exc:
        logger.warning("Company %s for citations unavailable (%s: %s)",
                       company_id, type(exc).__name__, exc)
    return None


def normalize_document_type(doc_type: Optional[str]) -> str:
    if not doc_type:
        return "report"
    doc_type = doc_type.strip().lower()
    mapping = {
        "report": "report",
        "filings": "filing",
        "policy": "policy",
        "dei-page": "dei-page",
        "others": "others",
    }
    return mapping.get(doc_type, doc_type)


def store_report(metadata: Dict[str, Any], company_id: int, num_pages: int, gcs_url: str, uid: str) -> tuple:
    """Store a report via INTERNAL endpoint. Returns (company_id_or_None, error_str_or_None)."""
    try:
        document_type = normalize_document_type(
            metadata.get("Type of Document", "report"))
        # Empty CSV cells arrive as pandas NaN (a float), which is not JSON-
        # serializable. Clean every string field through handle_metadata_value
        # (NaN -> "") and fall back to sensible defaults.
        payload = {
            "company_id": company_id,
            "company_name": handle_metadata_value(metadata.get("Company Name")),
            "year": _safe_year(metadata.get("Year")),
            "quarter": handle_metadata_value(metadata.get("Quarter")) or "Q4",
            "document_title": handle_metadata_value(metadata.get("Title of the Report")) or "Document",
            "document_url": gcs_url,
            "document_type": document_type,
            "report_type": handle_metadata_value(metadata.get("Type of Report")) or "report",
            "page_count": num_pages,
        }

        url = f"{DATAX_INTERNAL}/company-documents/{company_id}/"
        with _session() as s:
            response = s.post(url, json=payload,
                              headers=_headers(uid), timeout=60)

        if response.status_code == 201:
            return response.json().get("company_id"), None

        err = f"HTTP {response.status_code}: {response.text[:300]}"
        print(f"Report store failed: {err}")
        return None, err

    except Exception as e:
        print(f"Error in report store: {str(e)}")
        return None, str(e)


def fetch_reports_by_id(report_id: int, uid: str) -> Optional[Dict[str, Any]]:
    """Fetch a single report via INTERNAL endpoint."""
    try:
        url = f"{DATAX_INTERNAL}/company-documents/report/{report_id}"
        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=30)

        if response.status_code == 200:
            return response.json()

        print(
            f"Getting 'Report' failed: {response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in report get: {str(e)}")
        return None


def fetch_no_page_num_reports(uid: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch reports missing page numbers via INTERNAL endpoint."""
    try:
        url = f"{DATAX_INTERNAL}/company-documents/no-page-num/"
        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=60)

        if response.status_code == 200:
            return response.json()

        print(
            f"Getting 'Report (no-page-num)' failed: {response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in report get: {str(e)}")
        return None


def fetch_all_reports(uid: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch all reports via INTERNAL endpoint."""
    try:
        url = f"{DATAX_INTERNAL}/company-documents/"
        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=60)

        if response.status_code == 200:
            return response.json()

        print(
            f"Getting 'Report' failed: {response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in report get: {str(e)}")
        return None


def patch_report(report_id: int, update_value: Any, field: str, uid: str) -> bool:
    """Patch a report field via INTERNAL endpoint."""
    patch_url = f"{DATAX_INTERNAL}/company-documents/report/{report_id}"
    payload = {field: update_value}

    try:
        with _session() as s:
            resp = s.patch(patch_url, json=payload,
                           headers=_headers(uid), timeout=30)

        if resp.status_code == 200:
            return True
        else:
            print(
                f"Storing '{field}' failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"Error patching report field '{field}': {str(e)}")
        return False


def store_ingesting_report(metadata: Dict[str, Any], company_id: int, gcs_url: str, uid: str) -> tuple:
    """Store an ingestion report via INTERNAL endpoint. Returns (company_id_or_None, error_str_or_None)."""
    try:
        document_type = normalize_document_type(
            metadata.get("Type of Document", "report"))
        payload = {
            "company_id": company_id,
            "company_name": handle_metadata_value(metadata.get("Company Name")),
            "year": _safe_year(metadata.get("Year")),
            "quarter": "Q4",
            "document_title": handle_metadata_value(metadata.get("Title of the Report")) or "Document",
            "document_url": gcs_url,
            "document_type": document_type,
            "report_type": handle_metadata_value(metadata.get("Type of Report")) or "report",
        }

        url = f"{DATAX_INTERNAL}/ingestion-reports/{company_id}/"
        with _session() as s:
            response = s.post(url, json=payload,
                              headers=_headers(uid), timeout=60)

        if response.status_code == 201:
            return response.json().get("company_id"), None

        err = f"HTTP {response.status_code}: {response.text[:300]}"
        print(f"Ingesting Report store failed: {err}")
        return None, err

    except Exception as e:
        print(f"Error in report store: {str(e)}")
        return None, str(e)


def fetch_ingestion_reports_for_company(company_id: int, uid: str) -> Optional[List[Dict[str, Any]]]:
    """List ingestion reports for a company via INTERNAL endpoint."""
    try:
        url = f"{DATAX_INTERNAL}/ingestion-reports/{company_id}"
        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=60)

        if response.status_code == 200:
            return response.json()

        print(
            f"Getting 'ingesting Report' failed: {response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in report get: {str(e)}")
        return None


def fetch_reports_for_company(company_id: int, uid: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    List reports for a company via INTERNAL endpoint.
    If `year` is provided, it will be passed as a query parameter
    so that only that year's documents are returned.
    """
    try:
        base_url = f"{DATAX_INTERNAL}/company-documents/{company_id}"
        if year is not None:
            url = f"{base_url}?year={year}"
        else:
            url = base_url

        with _session() as s:
            response = s.get(url, headers=_headers(uid), timeout=60)

        if response.status_code == 200:
            return response.json()

        print(
            f"Getting company documents failed: {response.status_code} - {response.text}")
        return None

    except Exception as e:
        print(f"Error in fetch_reports_for_company: {e}")
        return None


# Lightweight document listing, added for batch/ingestion use.
#
# /company-documents/{id} returns every column including the analysis text —
# 1.41 MB for Barclays, of which `summary` alone is 69% and the fields we read
# are 4.3%. This endpoint selects 8 columns in SQL, so the analysis is never
# read out of Postgres, and it takes many company_ids at once.
#
# Env-overridable, and callers must tolerate None: it returns None when the
# endpoint is not deployed, so they can fall back to the per-company route.
DOCUMENT_METADATA_PATH = os.getenv(
    "DATAX_DOCUMENT_METADATA_PATH", "/company-documents/metadata")

# Server-side cap on ids per request.
DOCUMENT_METADATA_MAX_IDS = int(
    os.getenv("DATAX_DOCUMENT_METADATA_MAX_IDS", "200"))

# Set once the endpoint is found to be missing, so we stop issuing a doomed
# request before every fallback. Until it is deployed, /company-documents/
# {company_id} matches "metadata" as the path param and answers 422
# int_parsing — so absence looks like 422, not 404. Process-lifetime only:
# workers restart on deploy, which is when it would start existing.
_METADATA_UNAVAILABLE = False


def fetch_document_metadata(
    company_ids,
    uid: str,
    year: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Document metadata for several companies in one call, or None if unavailable.

    Returns a flat list; each row carries its own company_id, so the caller
    groups client-side rather than relying on ordering.

    Deliberately does NOT pass report_type. The stored values are messier than
    the four CITIES labels — 10-K, "Combined Pay Gap Report",
    "Sustainability Policy/framework" — and filtering server-side on exact
    names drops 23.9% of the documents the engine can actually use. The regex
    in ren/document_selection.canonical_report_type() does that job.

    None (not []) means "could not ask": a 404 because the endpoint is not
    deployed yet, or a transport failure. [] means "asked, nothing matched".
    """
    global _METADATA_UNAVAILABLE
    if _METADATA_UNAVAILABLE:
        return None

    ids = sorted({int(c) for c in company_ids if c is not None})
    if not ids:
        return []

    rows: List[Dict[str, Any]] = []
    for offset in range(0, len(ids), DOCUMENT_METADATA_MAX_IDS):
        chunk = ids[offset:offset + DOCUMENT_METADATA_MAX_IDS]
        params = {"company_ids": ",".join(str(c) for c in chunk)}
        if year is not None:
            params["year"] = year
        if year_from is not None:
            params["year_from"] = year_from
        if year_to is not None:
            params["year_to"] = year_to

        try:
            with _session() as s:
                response = s.get(f"{DATAX_INTERNAL}{DOCUMENT_METADATA_PATH}",
                                 headers=_headers(uid), params=params, timeout=90)
        except Exception as e:
            print(f"Error in fetch_document_metadata: {e}")
            return None

        if response.status_code in (404, 422):
            # Not deployed: either no such route (404), or the old
            # /{company_id} route caught "metadata" and could not parse it as
            # an int (422). Remember, so the next caller skips the attempt.
            logger.info("Document metadata endpoint unavailable (%s) — using "
                        "per-company reads for the rest of this process",
                        response.status_code)
            _METADATA_UNAVAILABLE = True
            return None
        if response.status_code != 200:
            print(f"Document metadata failed: {response.status_code} - "
                  f"{response.text[:200]}")
            return None

        payload = response.json()
        if not isinstance(payload, list):
            print(f"Document metadata returned {type(payload).__name__}, expected list")
            return None
        rows.extend(payload)

    return rows


def delete_ingestion_reports_for_company(company_id: int, uid: str) -> bool:
    """Delete all ingestion reports for a company via INTERNAL endpoint."""
    try:
        url = f"{DATAX_INTERNAL}/ingestion-reports/{company_id}"
        with _session() as s:
            response = s.delete(url, headers=_headers(uid), timeout=30)

        if response.status_code == 204:
            print(
                f"Successfully deleted ingestion reports for company {company_id}")
            return True

        print(
            f"Deleting 'ingesting Report' failed: {response.status_code} - {response.text}")
        return False

    except Exception as e:
        print(f"Error in report delete: {str(e)}")
        return False

# ---------- Data refinement helpers (unchanged) ----------


def refine_strategic_priority(priority: Dict[str, Any], company_id: int, year: int, quarter: str) -> Dict[str, Any]:
    refined: Dict[str, Any] = {
        "company_id": company_id,
        "priority_name": priority.get("priority_name", ""),
        "description": priority.get("description", ""),
        "dimension_focus": priority.get("dimension_focus", []),
        "year": year,
        "quarter": quarter,
    }
    timeframe = priority.get("timeframe", {}) or {}
    start_year_raw = timeframe.get("start_year")
    if start_year_raw is None or (isinstance(start_year_raw, str) and start_year_raw.strip().lower() in ("none", "")):
        refined["start_year"] = None
    else:
        try:
            refined["start_year"] = int(start_year_raw)
        except Exception:
            refined["start_year"] = None

    end_year_raw = timeframe.get("end_year")
    if end_year_raw is None or (isinstance(end_year_raw, str) and end_year_raw.strip().lower() in ("none", "")):
        refined["end_year"] = None
    else:
        try:
            refined["end_year"] = int(end_year_raw)
        except Exception:
            refined["end_year"] = None

    return refined


def refine_aspirational_goal(goal: Dict[str, Any], company_id: int, year: int, quarter: str) -> Dict[str, Any]:
    refined: Dict[str, Any] = {
        "company_id": company_id,
        "goal_name": goal.get("goal_name", ""),
        "description": goal.get("description", ""),
        "target_value": goal.get("target_value", ""),
        "status": goal.get("status", ""),
        "year": year,
        "quarter": quarter,
    }

    target_year_raw = goal.get("target_year")
    if target_year_raw is None or (isinstance(target_year_raw, str) and target_year_raw.strip().lower() in ("none", "")):
        refined["target_year"] = None
    else:
        try:
            refined["target_year"] = int(target_year_raw)
        except Exception:
            refined["target_year"] = None

    baseline = goal.get("baseline", {}) or {}
    refined["baseline_value"] = baseline.get("value")
    baseline_year_raw = baseline.get("year")
    if baseline_year_raw is None or (isinstance(baseline_year_raw, str) and baseline_year_raw.strip().lower() in ("none", "")):
        refined["baseline_year"] = None
    else:
        try:
            refined["baseline_year"] = int(baseline_year_raw)
        except Exception:
            refined["baseline_year"] = None

    return refined


def _make_json_safe(obj):
    """Recursively convert to JSON-serializable primitives."""
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()                 # "2023-01-01" / "2023-01-01T10:00:00"
    if isinstance(obj, Decimal):
        return float(obj)                      # or str(obj) if you prefer
    if obj == "None":
        return None
    return obj


def _normalize_list_of_strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if x is not None and str(x).strip()]
    if isinstance(value, str):
        s = value.strip()
        # Accept JSON-encoded array
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(x).strip() for x in arr if x is not None and str(x).strip()]
            except Exception:
                pass
        # Fallback: keep as a single item
        return [s]
    return [str(value).strip()]
