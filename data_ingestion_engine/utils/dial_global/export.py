# data_ingestion_engine/utils/dial_global/export.py
#
# Flattens DIAL Global scoring output into a leaderboard spreadsheet.
#
# One row per company-year. A company with 2023 and 2024 documents gets two
# rows; the several documents that fed a single year collapse into that year's
# one row, because the engine already extracts and scores a company-year as a
# unit.

import logging
import os
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from data_ingestion_engine.schemas.dial_global_schemas import (
    CATEGORY_WEIGHTS,
    QUESTION_CATALOGUE,
)
from data_ingestion_engine.utils.ren.company_group import (
    normalise_company_name,
)
from data_ingestion_engine.utils.dial_global.store_data import (
    STATUS_SCORED,
    fetch_dial_global_companies,
    fetch_all_dial_global_extractions,
)

logger = logging.getLogger(__name__)

CATEGORIES = list(CATEGORY_WEIGHTS.keys())

# Which company universe a row belongs to. DG ids index dial_global_companies,
# ReN ids index the canonical companies table; the two overlap numerically, so
# (universe, company_id) is the only safe key for a company.
DGI_UNIVERSE = "DG"
REN_UNIVERSE = "ReN"

# Concurrency for reading canonical ReN company names. One GET per company, so
# at full ReN scale this is ~1,100 short reads — worth parallelising, but not
# worth hammering DataX for.
REN_NAME_WORKERS = int(os.getenv("REN_NAME_WORKERS", "8"))

# Friendly headers for the metadata block of the All sheet, in the order the
# DIAL Global sample output uses.
METADATA_HEADERS = [
    ("q01_organisation_name", "Company"),
    ("q02_headquarters_city", "Headquarters City"),
    ("q03_primary_country_of_operation", "Country"),
    ("q04_primary_region_of_operation", "Region"),
    ("q05_ownership_type", "Ownership"),
    ("q06_sector", "Sector"),
    ("q07_industry", "Industry"),
    ("q08_board_pct_women", "Board % women"),
    ("q09_sr_leadership_pct_women", "Sr. Leadership % women"),
    ("q10_board_pct_ethnic_minority", "Board % ethnic minority"),
    ("q11_sr_leadership_pct_ethnic_minority", "Sr. Leadership % ethnic minority"),
    ("q12_total_number_of_employees", "# Employees"),
    ("q13_reporting_currency", "Reporting Currency"),
    ("q14_total_revenue", "Revenue"),
    ("q15_total_assets", "Total Assets"),
]

# Q16/Q17 are evaluation items but the sample surfaces them as named yes/no
# columns next to the metadata. Their justifications still appear in the
# numbered block below, so nothing is lost.
NAMED_EVALUATION_HEADERS = [
    ("q16_standalone_esg_report_publicly_available", "ESG Report Available?"),
    ("q17_esg_reporting_regulatory_requirement", "Regulated Reporting?"),
]


def _question_number(question_id: str) -> int:
    """q08_board_pct_women -> 8."""
    return int(question_id.split("_", 1)[0].lstrip("q"))


def _question_columns():
    """[(question_id, 'Q18_score', 'Q18_justification'), ...] in schema order."""
    return [(qid, f"Q{_question_number(qid)}_score",
             f"Q{_question_number(qid)}_justification")
            for qid in QUESTION_CATALOGUE]


# --- All sheet: one row per company-year, wide -------------------------------
ALL_COLUMNS = (
    ["Company", "Year"]
    + [header for _, header in METADATA_HEADERS if header != "Company"]
    + [header for _, header in NAMED_EVALUATION_HEADERS]
    + [c for _, score_col, just_col in _question_columns() for c in (score_col, just_col)]
    + ["Universe", "Company ID", "Documents", "Source Documents", "Extraction ID"]
)

# --- Scoring sheet: one row per company-year, the CITIES result ---------------
SCORING_COLUMNS = (
    ["Company", "Year"]
    + [c for cat in CATEGORIES
       for c in (f"{cat}_score", f"{cat}_weight", f"{cat}_weighted_score",
                 f"{cat}_score_rationale", f"{cat}_uncertainty_flagged",
                 f"{cat}_problematic_questions")]
    + ["total_score_percentage", "human_review_required", "flagged_categories",
       # Repeated-scoring evidence: how many times this company-year was scored,
       # the levels each run returned where they disagreed, and the spread
       # between the runs' totals. A wide spread means the score is soft.
       "score_runs", "disagreeing_levels", "score_spread",
       "Universe", "Company ID", "Documents", "Extraction ID"]
)


def _identity(record: Dict[str, Any], payload: Dict[str, Any],
              company_names: Optional[Dict[int, str]],
              ren_company_names: Optional[Dict[int, str]] = None):
    """(universe, company_id, year, display_name) for one stored record."""
    scoring = payload.get("scoring") or {}
    ren_id = record.get("ren_company_id")
    if ren_id is not None:
        universe, company_id = REN_UNIVERSE, ren_id
    else:
        universe, company_id = DGI_UNIVERSE, record.get("company_id")

    # A stored name beats the model's reading of it, and the two universes have
    # different sources of truth: the DIAL Global tracking workbook for DG ids,
    # the canonical companies table for ReN ids. Never cross them — the id
    # spaces overlap, so a DG lookup on a ReN id names the row after a different
    # company entirely.
    #
    # Falling back to the model's reading splits one company across several
    # rows, because it re-reads the name from each year's document: company_id
    # 38 arrived as both "ACCIONA" and "ACCIONA, S.A.", and 39 as "ACCOR" and
    # "Accor".
    name = None
    if company_id is not None:
        lookup = (ren_company_names if universe == REN_UNIVERSE else company_names)
        if lookup:
            name = lookup.get(int(company_id))
    name = name or scoring.get("organisation_name")

    year = record.get("year") or payload.get("reporting_year") or scoring.get("year")
    return universe, company_id, year, name


def _freshness(record: Dict[str, Any]):
    """
    Sort key for "which of these rows is the current one".

    updated_at first, because a re-run rewrites the row in place and only the
    timestamp moves; extraction_id breaks ties and covers rows with no
    timestamp. Both ascending, so max() picks the newest.
    """
    return (str(record.get("updated_at") or ""), record.get("extraction_id") or 0)


def select_records(
    records: Iterable[Dict[str, Any]],
    company_names: Optional[Dict[int, str]] = None,
    ren_company_names: Optional[Dict[int, str]] = None,
) -> "OrderedDict[tuple, tuple]":
    """
    One scored record per company-year, newest wins.

    The key is the company's NAME, not its id. Storage upserts on
    (ren_company_id, year), so a company that resolves to a different canonical
    entity between runs is written as a new row while the old one survives —
    the same business then appears twice in the leaderboard under two entity
    ids. That is not hypothetical: AAC Clyde Space, Acarix, AIB Group, Airbus,
    AmRest and Cisco are each stored under two ids today.

    Keying on the normalised name collapses those, and `_freshness` keeps the
    most recently written row — which is the one the latest run produced.

    Rows whose name is unknown fall back to the id, so a failed name lookup
    degrades to the old behaviour rather than merging unrelated companies.
    """
    best: Dict[tuple, tuple] = {}
    collapsed: Dict[tuple, set] = {}

    for record in records:
        payload = record.get("extracted_data") or {}
        if not payload.get("scoring"):
            # Unscored rows have no CITIES result to report.
            continue
        universe, company_id, year, name = _identity(
            record, payload, company_names, ren_company_names)

        identity = normalise_company_name(name) or None
        key = (universe, identity or f"id:{company_id}", year)

        collapsed.setdefault(key, set()).add(company_id)
        incumbent = best.get(key)
        if incumbent is None or _freshness(record) > _freshness(incumbent[0]):
            # company_id travels in the VALUE, not the key. The key is the
            # normalised name (that is the point — it collapses one company
            # stored under several entity ids), so reading an id out of the key
            # yields the name instead and the sheet's "Company ID" column fills
            # with strings like "abbvieinc".
            best[key] = (record, payload, name, company_id)

    duplicates = {k: v for k, v in collapsed.items() if len(v) > 1}
    if duplicates:
        logger.warning(
            "[export] %s company-year(s) existed under more than one entity id; "
            "kept the most recently written row for each. Examples: %s",
            len(duplicates),
            "; ".join(f"{k[0]} {k[2]} {sorted(v)}"
                      for k, v in list(duplicates.items())[:5]))

    return OrderedDict(
        sorted(best.items(), key=lambda kv: (str(kv[1][2] or ""), -(kv[0][2] or 0))))


def build_all_rows(
    records: Iterable[Dict[str, Any]],
    company_names: Optional[Dict[int, str]] = None,
    ren_company_names: Optional[Dict[int, str]] = None,
) -> List[Dict[str, Any]]:
    """The extraction JSON, one row per company-year, wide."""
    rows = []
    for (universe, _identity_key, year), (record, payload, name, company_id) in \
            select_records(records, company_names, ren_company_names).items():
        extraction = payload.get("extraction") or {}
        organisation = extraction.get("organisation") or {}

        row: Dict[str, Any] = {"Company": name, "Year": year}
        for field, header in METADATA_HEADERS:
            if header == "Company":
                continue
            row[header] = organisation.get(field)

        for field, header in NAMED_EVALUATION_HEADERS:
            row[header] = (organisation.get(field) or {}).get("score")

        for question_id, score_col, just_col in _question_columns():
            section = QUESTION_CATALOGUE[question_id][0]
            item = (extraction.get(section) or {}).get(question_id) or {}
            row[score_col] = item.get("score")
            row[just_col] = item.get("justification")

        row.update({
            "Universe": universe,
            "Company ID": company_id,
            "Documents": payload.get("document_count"),
            "Source Documents": ", ".join(
                str(d.get("document_type") or "Document")
                for d in (payload.get("source_documents") or [])),
            "Extraction ID": record.get("extraction_id"),
        })
        rows.append(row)
    return rows


def build_scoring_rows(
    records: Iterable[Dict[str, Any]],
    company_names: Optional[Dict[int, str]] = None,
    ren_company_names: Optional[Dict[int, str]] = None,
) -> List[Dict[str, Any]]:
    """The scoring JSON, one row per company-year."""
    rows = []
    for (universe, _identity_key, year), (record, payload, name, company_id) in \
            select_records(records, company_names, ren_company_names).items():
        scoring = payload.get("scoring") or {}
        categories = scoring.get("categories") or {}

        row: Dict[str, Any] = {"Company": name, "Year": year}
        flagged = []
        for category in CATEGORIES:
            block = categories.get(category) or {}
            row[f"{category}_score"] = block.get("score")
            row[f"{category}_weight"] = block.get("category_weight_percent")
            row[f"{category}_weighted_score"] = block.get("weighted_score")
            row[f"{category}_score_rationale"] = block.get("score_rationale")
            row[f"{category}_uncertainty_flagged"] = bool(block.get("uncertainty_flagged"))
            row[f"{category}_problematic_questions"] = ", ".join(
                block.get("problematic_questions") or [])
            if block.get("uncertainty_flagged"):
                flagged.append(category)

        # How far the repeated scoring runs disagreed. Absent on rows written
        # before consensus scoring, and on any row scored a single time.
        consensus = payload.get("scoring_consensus") or {}
        observed = consensus.get("observed_levels") or {}
        disagreeing = "; ".join(
            f"{category} {','.join(str(level) for level in levels)}"
            for category, levels in observed.items() if len(set(levels)) > 1)

        # Derived, not stored: the scoring schema dropped these, but a reviewer
        # still needs to see at a glance which rows need a human.
        row.update({
            "total_score_percentage": (scoring.get("overall_score") or {}).get(
                "total_score_percentage"),
            "human_review_required": bool(flagged),
            "flagged_categories": ", ".join(flagged),
            "score_runs": consensus.get("runs_completed"),
            "disagreeing_levels": disagreeing,
            "score_spread": consensus.get("total_spread"),
            "Universe": universe,
            "Company ID": company_id,
            "Documents": payload.get("document_count"),
            "Extraction ID": record.get("extraction_id"),
        })
        rows.append(row)
    return rows


# Control characters openpyxl rejects: everything below 0x20 except tab (0x09),
# newline (0x0A) and carriage return (0x0D).
_ILLEGAL_XLSX_CHARS = re.compile(r"[\000-\010\013\014\016-\037]")


def _strip_illegal(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Remove control characters Excel refuses to store.

    Justifications are verbatim quotes lifted out of PDFs, so they occasionally
    carry a stray control character from the source document's encoding —
    Husqvarna's 2022 Q17 quote had \\x17 where the source wrote "Chapter 6, §12",
    the section symbol having been mangled on extraction. openpyxl raises
    IllegalCharacterError on the first one and the whole workbook fails to write,
    after every model call has already been paid for.

    Deliberately does NOT test the column dtype. It used to check
    `dtype == object`, which silently stopped matching when pandas began giving
    string columns their own dtype — the guard never fired, nothing was ever
    stripped, and the protection this function exists to provide was gone. The
    isinstance check inside the map is the only test needed; running it over the
    numeric columns too costs a fraction of a second on a full export and cannot
    rot the same way.
    """
    removed = 0

    def clean(value):
        nonlocal removed
        if not isinstance(value, str):
            return value
        stripped = _ILLEGAL_XLSX_CHARS.sub("", value)
        if stripped != value:
            removed += 1
        return stripped

    for column in frame.columns:
        frame[column] = frame[column].map(clean)

    if removed:
        logger.info(
            "[export] Stripped control characters from %s cell(s) — quotes "
            "lifted from PDFs occasionally carry them", removed)
    return frame


def _int_columns(frame: pd.DataFrame, columns) -> pd.DataFrame:
    """Nullable Int64 so a null score reads as blank, not '1.0'."""
    for column in columns:
        if column in frame:
            frame[column] = frame[column].astype("Int64")
    return frame


def build_workbook(
    records: Iterable[Dict[str, Any]],
    company_names: Optional[Dict[int, str]] = None,
    ren_company_names: Optional[Dict[int, str]] = None,
) -> "OrderedDict[str, pd.DataFrame]":
    """
    The two output sheets.

      Scoring — the scoring JSON: rubric level, weight, weighted score,
                rationale and uncertainty flags per category, plus the total
      All     — the extraction JSON: profile and baseline metrics, then a
                score/justification pair for every question Q16-Q79
    """
    records = list(records)

    scoring = pd.DataFrame(
        build_scoring_rows(records, company_names, ren_company_names),
        columns=SCORING_COLUMNS)
    scoring = _int_columns(scoring, [f"{c}_score" for c in CATEGORIES])

    everything = pd.DataFrame(
        build_all_rows(records, company_names, ren_company_names),
        columns=ALL_COLUMNS)
    everything = _int_columns(
        everything,
        [h for _, h in NAMED_EVALUATION_HEADERS]
        + [score_col for _, score_col, _ in _question_columns()]
        + ["# Employees"])

    return OrderedDict([("Scoring", _strip_illegal(scoring)),
                        ("All", _strip_illegal(everything))])


def fetch_ren_company_names(
    uid: str,
    company_ids: Iterable[int],
) -> Dict[int, str]:
    """
    Canonical names for ReN company ids, read one row at a time.

    ReN has no curated name sheet the way DIAL Global does, so the canonical
    `companies` row is the source of truth. Ids that cannot be read are simply
    absent from the map and fall back to the model's reading — a missing name is
    worth far less than a failed export.
    """
    from data_ingestion_engine.utils.store_data import fetch_company

    ids = sorted({int(c) for c in company_ids if c is not None})
    if not ids:
        return {}

    names: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=REN_NAME_WORKERS) as pool:
        futures = {pool.submit(fetch_company, cid, uid): cid for cid in ids}
        for future in as_completed(futures):
            company_id = futures[future]
            try:
                company = future.result()
            except Exception as e:
                logger.warning("[ren] Could not read company %s: %s", company_id, e)
                continue
            name = (company or {}).get("name") or (company or {}).get("company_name")
            if name:
                names[company_id] = name

    missing = len(ids) - len(names)
    if missing:
        logger.warning(
            "[ren] %s of %s canonical company names unavailable — those rows keep "
            "the model's reading of the name", missing, len(ids))
    return names


def fetch_workbook(uid: str) -> "OrderedDict[str, pd.DataFrame]":
    """Pull every DIAL Global extraction from DataX and build both sheets."""
    records = list(fetch_all_dial_global_extractions(uid))
    company_names = {
        int(c["company_id"]): c.get("company_name")
        for c in fetch_dial_global_companies(uid)
        if c.get("company_id") is not None
    }
    ren_company_names = fetch_ren_company_names(
        uid, (r.get("ren_company_id") for r in records))
    return build_workbook(records, company_names, ren_company_names)


def fetch_leaderboard_dataframe(uid: str) -> pd.DataFrame:
    """Just the Scoring sheet."""
    return fetch_workbook(uid)["Scoring"]


def write_leaderboard(frames, path: str) -> str:
    """
    Write to .xlsx or .csv, chosen by the file extension.

    `frames` may be a single DataFrame or a {sheet_name: DataFrame} mapping.
    An .xlsx gets one tab per frame; a .csv can only hold one table, so the
    first frame is written and the rest are skipped with a warning.
    """
    if isinstance(frames, pd.DataFrame):
        frames = OrderedDict([("Scoring", frames)])

    if path.lower().endswith(".csv"):
        name, frame = next(iter(frames.items()))
        if len(frames) > 1:
            logger.warning(
                "CSV holds one table — wrote '%s', skipped %s. Use .xlsx for both.",
                name, ", ".join(list(frames)[1:]))
        frame.to_csv(path, index=False)
        return path

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in frames.items():
            frame.to_excel(writer, index=False, sheet_name=name)
    logger.info("Wrote workbook -> %s (%s)", path,
                ", ".join(f"{n}: {len(f)} rows" for n, f in frames.items()))
    return path
