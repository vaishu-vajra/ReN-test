# data_ingestion_engine/utils/ren/company_list.py
#
# Reads the company cohort spreadsheet ("DGI FINAL LIST ....xlsx") and turns it
# into rows the fan-out can resolve against the canonical `companies` table.
#
# Expected columns (matched by alias, whitespace-insensitive):
#   Organisation | Listed | TICKER | EXCHANGE | Market Cap | Sector | Region/Country
#
# Resolution uses ticker + exchange, not just the name. A trigram match on
# "Admiral" vs "ADMIRAL Group" is a coin flip; ADM on LSE - UK is not.

import logging
import os
import re
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

COHORT_COLUMN_ALIASES = {
    "company_name": ["Organisation", "Organization", "Company Name", "Company"],
    "listed": ["Listed", "Is Listed"],
    "ticker": ["TICKER", "Ticker", "Ticker Symbol", "Symbol"],
    "exchange": ["EXCHANGE", "Exchange"],
    "isin": ["ISIN", "ISINs", "Isin"],
    "market_cap": ["Market Cap", "Market Capitalisation", "Market Capitalization"],
    "sector": ["Sector", "GICS Sector"],
    "industry": ["Industry", "GICS Industry"],
    "region": ["Region/Country", "Region / Country", "Region", "Country"],
}

# The Region/Country column mixes ISO-3166 alpha-2 codes (SE, NO, GB, BE, DE)
# with region buckets (AMERICAS, EMEA, APAC, UK&I). Only the former is a
# country, and sending "AMERICAS" as one would corrupt the canonical row, so
# regions are carried separately and never forwarded as `country`.
_REGION_BUCKETS = {"AMERICAS", "EMEA", "APAC", "UK&I", "UKI", "GLOBAL", "ROW"}


def _normalise_keys(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).strip(): v for k, v in row.items()}


def _get(row_norm: Dict[str, Any], field: str) -> Optional[str]:
    for alias in COHORT_COLUMN_ALIASES.get(field, []):
        if alias not in row_norm:
            continue
        value = row_norm[alias]
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "none"):
            return text
    return None


def split_region(value: Optional[str]) -> tuple:
    """
    Split the Region/Country cell into (country, region).

    A two-letter code is treated as a country; a known bucket as a region;
    anything else is kept as a region so nothing is silently dropped.
    """
    text = (value or "").strip()
    if not text:
        return None, None
    upper = text.upper()
    if upper in _REGION_BUCKETS:
        return None, upper
    if re.fullmatch(r"[A-Z]{2}", upper):
        return upper, None
    return None, text


def read_cohort_rows(local_path: str) -> List[Dict[str, Any]]:
    """Read the cohort sheet (.xlsx or .csv) into raw dict rows."""
    ext = os.path.splitext(local_path)[1].lower()
    if ext == ".csv":
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                df = pd.read_csv(local_path, encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"Could not decode {local_path} as CSV")
    else:
        # First sheet only — the cohort is a single tab, unlike the DIAL Global
        # tracking workbook.
        df = pd.read_excel(local_path, sheet_name=0)

    df = df.dropna(how="all")
    return df.to_dict(orient="records")


def parse_cohort(
    rows: List[Dict[str, Any]],
    listed_only: bool = False,
) -> tuple:
    """
    Normalise cohort rows and drop duplicates.

    Returns (companies, skipped). Each company is
        {company_name, ticker, exchange, sector, country, region, market_cap, listed}

    De-duplication is by (ticker, exchange) when a ticker is present, else by
    lowercased name. The sheet legitimately contains near-duplicate names for
    the same listing — "Admiral" and "ADMIRAL Group" are both ADM on LSE - UK —
    and running both would double-count one company in the leaderboard.
    """
    companies: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen: Dict[tuple, str] = {}

    for row in rows:
        row_norm = _normalise_keys(row)

        name = _get(row_norm, "company_name")
        if not name:
            continue

        listed = _get(row_norm, "listed")
        if listed_only and (listed or "").strip().lower() not in ("yes", "y", "true"):
            skipped.append({"company_name": name, "reason": "not listed"})
            continue

        ticker = _get(row_norm, "ticker")
        exchange = _get(row_norm, "exchange")
        isin = _get(row_norm, "isin")
        country, region = split_region(_get(row_norm, "region"))

        # ISIN identifies a security unambiguously, so it beats (ticker,
        # exchange) — which collides whenever a sheet writes the same listing
        # with different symbol or exchange spellings.
        if isin:
            key = (isin.split(",")[0].strip().upper(), "isin")
        elif ticker:
            key = (ticker.upper(), (exchange or "").upper())
        else:
            key = (name.strip().lower(), "")
        if key in seen:
            skipped.append({
                "company_name": name,
                "reason": f"duplicate of '{seen[key]}' ({ticker or name})",
            })
            continue
        seen[key] = name

        companies.append({
            "company_name": name,
            "ticker": ticker,
            "exchange": exchange,
            "isin": isin,
            "sector": _get(row_norm, "sector"),
            "industry": _get(row_norm, "industry"),
            "country": country,
            "region": region,
            "market_cap": _get(row_norm, "market_cap"),
            "listed": listed,
        })

    return companies, skipped


def load_cohort(local_path: str, listed_only: bool = False) -> tuple:
    """Read and normalise a cohort sheet in one call."""
    companies, skipped = parse_cohort(read_cohort_rows(local_path), listed_only)
    logger.info("[cohort] %s companies, %s skipped from %s",
                len(companies), len(skipped), os.path.basename(local_path))
    return companies, skipped


def to_upsert_metadata(company: Dict[str, Any]) -> Dict[str, Any]:
    """
    Shape one cohort row for get_company_id().

    `country` is only ever a real ISO code — see split_region(). Region buckets
    stay out of the payload so DataX never stores "AMERICAS" as a country.
    """
    return {
        "Company Name": company.get("company_name", ""),
        "Ticker Symbol": company.get("ticker") or "",
        "Exchange": company.get("exchange") or "",
        "ISIN": company.get("isin") or "",
        "Sector": company.get("sector") or "",
        "Industry": company.get("industry") or "",
        "Country": company.get("country") or "",
    }


# Resolution strategies, most reliable first. Measured over the 756-company
# ingestion mastersheet against dev:
#
#   isin alone                    568 companies resolved to one holding documents
#   isin + ticker + exchange      419
#   best of both                  580
#
# ISIN alone wins because the canonical table holds duplicate companies that
# share an ISIN, and sending extra identifiers steers matching onto the empty
# twin — AbbVie resolves to id 29351 (0 documents) with ticker and exchange
# attached, and to id 25 (244 documents) on ISIN alone. The extra 12 companies
# that only the full payload finds are why the fallback is kept.
_RESOLUTION_STRATEGIES = (
    ("isin", ("ISIN",)),
    ("isin+ticker+exchange", ("ISIN", "Ticker Symbol", "Exchange",
                              "Sector", "Industry", "Country")),
    ("name+ticker+exchange", ("Company Name", "Ticker Symbol", "Exchange")),
)


def resolve_cohort_row(
    company: Dict[str, Any],
    uid: str,
    has_documents: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Resolve one cohort row to the canonical company that holds its documents.

    Never creates a company: every attempt goes through resolve_company_id().

    Each strategy in _RESOLUTION_STRATEGIES is tried in turn and the first one
    that lands on a company WITH documents wins. That check is the point of the
    function — resolution succeeding is not the same as resolution being right,
    because a duplicate row resolves perfectly well and then yields nothing to
    extract. Where no strategy finds documents, the first id that resolved at
    all is returned so the caller can tell "matched but empty" apart from
    "no match at all".

    `has_documents` is a callable (company_id, uid) -> bool, injected so this
    can be unit-tested without DataX. Defaults to a real document fetch.

    Returns {company_id, strategy, matched, documents}.
    """
    # Imported here: utils.store_data pulls in the request stack, and this
    # module is also used by offline sheet-parsing that should not need it.
    from data_ingestion_engine.utils.store_data import (
        fetch_reports_for_company, resolve_company_id)
    from data_ingestion_engine.utils.ren.company_group import (
        fetch_reports_for_company_group)

    if has_documents is None:
        # One document fetch is expensive: DataX ignores limit/page_size on
        # /company-documents/{id} and always returns the company's whole list —
        # 1.4 MB for Barclays. Checking the name group up front meant up to 12
        # of those per strategy per cohort row, which OOM-killed the web worker
        # on a 937-row cohort.
        #
        # So: ask the resolved entity first, and only fall back to the group
        # when it is empty. That is the case the group exists for — filings
        # ingested under a sibling entity — and it is rare (24 of 937 on the
        # last prod run), so the expensive path stays bounded.
        seen: Dict[int, bool] = {}

        def has_documents(company_id: int, user_id: str) -> bool:
            if company_id in seen:
                return seen[company_id]

            payload = fetch_reports_for_company(company_id, user_id) or {}
            found = bool(payload.get("reports") or payload.get("filings"))

            if not found:
                group = fetch_reports_for_company_group(company_id, user_id)
                found = bool(group.get("reports") or group.get("filings"))

            seen[company_id] = found
            return found

    metadata = to_upsert_metadata(company)
    first_match: Optional[int] = None
    first_strategy: Optional[str] = None

    for strategy, fields in _RESOLUTION_STRATEGIES:
        payload = {field: metadata.get(field, "") for field in fields}
        if not any(str(v).strip() for v in payload.values()):
            continue

        company_id = resolve_company_id(payload, uid)
        if not company_id:
            continue

        if first_match is None:
            first_match, first_strategy = company_id, strategy

        if has_documents(company_id, uid):
            return {"company_id": company_id, "strategy": strategy,
                    "matched": True, "documents": True}

    return {"company_id": first_match, "strategy": first_strategy,
            "matched": first_match is not None, "documents": False}


# ---------------------------------------------------------------------------
# The flagged-pair cohort
# ---------------------------------------------------------------------------
#
# The progressive re-run targets specific company-years rather than companies.
# Its cohort is a leaderboard export filtered on score — every row in
# "Problem Companies Full List - Sheet2.csv" is a ReN company-year that was
# scored on exactly ONE document, which is the defect being repaired.
#
# Company ids come straight from the export's `Company ID` column, so they are
# already canonical companies.company_id values: no name/ISIN resolution is
# needed, and none should be attempted. Re-resolving would risk landing on the
# duplicate empty twin that the ISIN-only strategy exists to avoid.

FLAGGED_PAIR_COLUMNS = {
    "company_id": ["Company ID", "company_id", "ren_company_id"],
    "year": ["Year", "year"],
    "score": ["total_score_percentage", "Total Score Percentage", "score"],
    "company_name": ["Company", "Company Name", "company_name"],
    "universe": ["Universe", "universe"],
}


def _first_column(row: Dict[str, Any], field: str) -> Optional[str]:
    for alias in FLAGGED_PAIR_COLUMNS.get(field, []):
        if alias in row and row[alias] is not None:
            text = str(row[alias]).strip()
            if text and text.lower() not in ("nan", "none"):
                return text
    return None


def load_flagged_pairs(
    csv_path: str,
    max_score: float = 70.0,
    universe: Optional[str] = "ReN",
) -> tuple:
    """
    (company_id, year) pairs from a scored leaderboard export, filtered on score.

    Returns (pairs, skipped) where `pairs` is a list of
    {company_id, year, company_name, baseline_score} de-duplicated on
    (company_id, year), and `skipped` records every row dropped and why — an
    unparseable id or year, a score above the threshold, or the wrong universe.

    `universe` defaults to "ReN" because the progressive pipeline only knows how
    to read canonical company documents; a DG row's `Company ID` indexes
    dial_global_companies and would name a different company entirely.
    """
    import csv as _csv

    pairs: Dict[tuple, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for index, raw in enumerate(_csv.DictReader(handle), start=2):
            row = _normalise_keys(raw)
            entry = {"row": index, "company_name": _first_column(row, "company_name")}

            row_universe = _first_column(row, "universe")
            if universe and row_universe and row_universe != universe:
                skipped.append({**entry, "reason": f"universe={row_universe}"})
                continue

            try:
                company_id = int(float(_first_column(row, "company_id")))
                year = int(float(_first_column(row, "year")))
            except (TypeError, ValueError):
                skipped.append({**entry, "reason": "unparseable company_id or year"})
                continue

            score_text = _first_column(row, "score")
            try:
                score = float(score_text)
            except (TypeError, ValueError):
                skipped.append({**entry, "reason": "unparseable score"})
                continue

            if score > max_score:
                skipped.append({**entry, "reason": f"score={score} > {max_score}"})
                continue

            key = (company_id, year)
            if key in pairs:
                # The same company-year can appear twice when it was stored
                # under two entity ids. Keep the worse score — it is the row the
                # re-run is meant to repair.
                if score < pairs[key]["baseline_score"]:
                    pairs[key]["baseline_score"] = score
                continue

            pairs[key] = {
                "company_id": company_id,
                "year": year,
                "company_name": entry["company_name"],
                "baseline_score": score,
            }

    ordered = sorted(pairs.values(), key=lambda p: (p["company_id"], -p["year"]))
    logger.info("[ren] %s flagged company-year(s) at score <= %s (%s row(s) skipped)",
                len(ordered), max_score, len(skipped))
    return ordered, skipped
