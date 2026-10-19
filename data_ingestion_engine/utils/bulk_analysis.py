"""
Shared pieces for the bulk risk / gap / earnings-delta run.

Two scripts sit on top of this module:

  scripts/build_analysis_cohort.py   resolve the company universe + documents
  scripts/run_bulk_analysis.py       spend the model calls that cohort implies

The split is deliberate. The cohort step is free (GETs only) and produces a
reviewable file; the run step is the one that costs money and overwrites stored
analysis, so it never decides for itself what to work on.
"""

import datetime
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bulk_analysis")

# --- Document types in scope -------------------------------------------------
#
# These five are the ones asked for. NOT reusing ren/document_selection's
# canonical_report_type(): that classifier serves the CITIES evidence base, it
# folds 10-K and 20-F into a single ANNUAL_REPORT label, has no 10-Q or
# transcript pattern at all, and its ordering is a contract for the ReN engine
# that must not be edited for this.
EARNINGS_TRANSCRIPT = "Earnings Call Transcript"
TEN_K = "10-K"
TEN_Q = "10-Q"
TWENTY_F = "20-F"
ANNUAL_REPORT = "Annual Report"

IN_SCOPE_TYPES = (EARNINGS_TRANSCRIPT, TEN_K, TEN_Q, TWENTY_F, ANNUAL_REPORT)

# Matched in order, first hit wins — narrow to broad.
_TYPE_PATTERNS = [
    # Before every form pattern: an "Q3 2025 Earnings Call Transcript" filed
    # against a 10-Q is a transcript, and transcripts are the only input the
    # earnings delta can use.
    (EARNINGS_TRANSCRIPT, re.compile(
        r"earnings\s*(?:call|transcript|conference)|"
        r"(?:call|conference)\s*transcript|\btranscript\b|"
        r"earnings\s*call", re.I)),

    # 10-Q before 10-K so "10-Q" never falls into a looser 10-K match.
    (TEN_Q, re.compile(r"\b10[-\s_]?q\b|quarterly\s*report\s*on\s*form", re.I)),
    (TEN_K, re.compile(r"\b10[-\s_]?k\b", re.I)),
    (TWENTY_F, re.compile(r"\b20[-\s_]?f\b", re.I)),

    (ANNUAL_REPORT, re.compile(
        r"annual\s*(?:financial\s*|integrated\s*)?report|"
        r"annual\s*(?:&|and)\s*accounts|"
        r"universal\s*registration\s*document", re.I)),
]

# Descending confidence, same rationale as the ReN classifier: report_type is the
# curated label, the title recovers rows typed "Others", and document_type is a
# storage bucket that carries almost no signal.
_TYPE_FIELDS = ("report_type", "document_title", "document_type")


def classify_document(doc: Dict[str, Any]) -> Optional[str]:
    """One of IN_SCOPE_TYPES, or None for a document that is out of scope."""
    for field in _TYPE_FIELDS:
        value = doc.get(field)
        if not value:
            continue
        for label, pattern in _TYPE_PATTERNS:
            if pattern.search(str(value)):
                return label
    return None


# --- Periods -----------------------------------------------------------------

_QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4,
                  "H1": 2, "H2": 4, "S1": 2, "S2": 4,
                  "FY": 4, "": 4}

# A quarter is not always stored on the row; the title usually still says it.
_QUARTER_IN_TITLE = re.compile(r"\b(Q[1-4]|H[12]|S[12]|FY)\b", re.I)


def document_quarter(doc: Dict[str, Any]) -> str:
    """
    The reporting period label, falling back to the title.

    The lightweight metadata endpoint does not always carry `quarter`, and the
    earnings delta cannot order a window — or name a year-ago baseline — without
    one. A transcript with no recoverable period sorts last within its year.
    """
    quarter = str(doc.get("quarter") or "").strip().upper()
    if quarter:
        return quarter
    match = _QUARTER_IN_TITLE.search(str(doc.get("document_title") or ""))
    return match.group(1).upper() if match else ""


def document_year(doc: Dict[str, Any]) -> Optional[int]:
    """Reporting year, tolerating '2025', 2025 and '2025.0'."""
    try:
        return int(float(doc.get("year")))
    except (TypeError, ValueError):
        return None


def period_sort_key(doc: Dict[str, Any]) -> tuple:
    """Chronological order, oldest first."""
    year = document_year(doc)
    quarter = document_quarter(doc)
    return (year if year is not None else -1,
            _QUARTER_ORDER.get(quarter, 4),
            doc.get("report_id") or 0)


def default_end_year(today: Optional[datetime.date] = None) -> int:
    """
    The most recent reporting year to treat as in scope.

    Reporting years run ahead of the calendar: a fiscal year ending in early
    2027 is legitimately filed as 2027 during 2026, and Q4 of the current year
    is published while that year is still running. So the window ends at the
    current calendar year, not the last completed one.
    """
    return (today or datetime.date.today()).year


# --- Turning documents into units of work ------------------------------------

# The earnings-delta prompt takes a trailing window of 2-5 transcripts, oldest
# first. Mirrors EARNINGS_DELTA_MIN_DOCS / _MAX_DOCS in api/views/gemini.py.
DELTA_MIN_DOCS = 2
DELTA_MAX_DOCS = 5

PER_DOCUMENT_SECTIONS = ("risk_analysis", "gap_analysis")


def normalise_documents(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Flatten raw document rows to in-scope documents, dropping anything with no
    URL, no usable id, or a type outside IN_SCOPE_TYPES. De-duplicates on
    report_id: the same filing is sometimes ingested under more than one entity.
    """
    documents: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or []:
        report_id = row.get("report_id")
        url = row.get("document_url")
        if report_id is None or not url or report_id in seen:
            continue
        doc_type = classify_document(row)
        if not doc_type:
            continue
        seen.add(report_id)
        documents.append({
            "report_id": report_id,
            "company_id": row.get("company_id"),
            "year": document_year(row),
            "quarter": document_quarter(row),
            "document_type": doc_type,
            "document_url": url,
            # Kept so an audit can see what the classification was made from.
            "report_type": row.get("report_type"),
            "document_title": row.get("document_title"),
        })
    return documents


def unflatten_envelope(payload: Any, company_id: int) -> List[Dict[str, Any]]:
    """
    fetch_reports_for_company() returns a four-bucket envelope; the metadata
    endpoint returns a flat list. Accept either.

    All four buckets are read. `others` holds rows whose report_type was
    recorded as "Others" but whose title classifies perfectly well.
    """
    if not payload:
        return []
    if isinstance(payload, dict):
        rows = [*(payload.get("reports") or []),
                *(payload.get("filings") or []),
                *(payload.get("policies") or []),
                *(payload.get("others") or [])]
    else:
        rows = list(payload)
    for row in rows:
        row.setdefault("company_id", company_id)
    return rows


def build_work_units(
    documents: List[Dict[str, Any]],
    start_year: int,
    end_year: int,
    sections: "tuple" = PER_DOCUMENT_SECTIONS,
    include_delta: bool = True,
) -> List[Dict[str, Any]]:
    """
    The model calls implied by one company's documents.

    Two shapes come out:

      {"kind": "document", "section": ..., "report_id": ...}
          One in-scope document in [start_year, end_year] -> one call, saved on
          that document's row.

      {"kind": "delta", "report_ids": [oldest, ..., target]}
          One TRANSCRIPT in [start_year, end_year] -> one call over a rolling
          trailing window of up to DELTA_MAX_DOCS transcripts, saved on the
          target. The preceding transcripts in the window are allowed to be
          OLDER than start_year: they are baselines, not targets, and a Q1 2025
          delta with no 2024 history to compare against is worthless. This is
          why the cohort is built with a year of extra history.
    """
    units: List[Dict[str, Any]] = []
    in_window = [d for d in documents
                 if d["year"] is not None and start_year <= d["year"] <= end_year]

    for doc in sorted(in_window, key=period_sort_key):
        for section in sections:
            units.append({
                "kind": "document",
                "section": section,
                "company_id": doc.get("company_id"),
                "report_id": doc["report_id"],
                "document_type": doc["document_type"],
                "year": doc["year"],
                "quarter": doc["quarter"],
                "document_title": doc.get("document_title"),
            })

    if not include_delta:
        return units

    transcripts = sorted(
        (d for d in documents if d["document_type"] == EARNINGS_TRANSCRIPT),
        key=period_sort_key)

    for index, target in enumerate(transcripts):
        if target["year"] is None or not (start_year <= target["year"] <= end_year):
            continue
        window = transcripts[max(0, index + 1 - DELTA_MAX_DOCS): index + 1]
        if len(window) < DELTA_MIN_DOCS:
            # Nothing to compare the target against. Skipped rather than sent:
            # the endpoint rejects a lone document for the same reason.
            continue
        units.append({
            "kind": "delta",
            "section": "earnings_delta",
            "company_id": target.get("company_id"),
            "report_id": target["report_id"],
            "report_ids": [d["report_id"] for d in window],
            # The period of each document in the window, carried so the runner
            # can rebuild the model's `Doc N: Period [ROLE]` labels without
            # re-reading the report rows. Those rows hold the full analysis text
            # — over a megabyte for a large company — and a run that fetched one
            # per window document would spend its memory and its round trips on
            # data it needs two fields from.
            "window": [{"report_id": d["report_id"], "year": d["year"],
                        "quarter": d["quarter"]} for d in window],
            "periods": [f"{d['quarter']} {d['year']}".strip() for d in window],
            "year": target["year"],
            "quarter": target["quarter"],
            "document_type": target["document_type"],
        })

    return units
