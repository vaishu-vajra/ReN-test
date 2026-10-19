#!/usr/bin/env python
"""
Snapshot the stored ReN rows for a cohort of company-years, BEFORE re-running.

Storage upserts on (ren_company_id, year): a progressive re-run REPLACES the
existing scored row in place, and DataX keeps no history. This snapshot is
therefore both the only rollback path and the before-side of the final delta.

Read-only. It issues GETs and writes one local JSON file; it never writes to
DataX. Run it and confirm the row count before dispatching anything.

Reads year by year rather than in one sweep. `/internal/dial-global/extractions/`
500s unfiltered on prod — every variant, including ?limit=5, in ~2.6s — but
filtered reads work fine, so the cohort's distinct years are fetched separately
and unioned on extraction_id.

Usage:
    # snapshot the flagged pairs from the problem-companies export
    python -m data_ingestion_engine.scripts.snapshot_ren_baseline \
        --uid <uid> \
        --api-url https://<prod-datax-host> \
        --out ren_baseline_2026-08-25.json

    # or an explicit pair list
    python -m data_ingestion_engine.scripts.snapshot_ren_baseline \
        --uid <uid> --pairs 25:2024,58:2026 --out baseline.json

--api-url overrides DATAX_API_URL for this process only; without it the script
targets whatever .env points at (currently the dev backend).
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

# --api-url has to win before store_data is imported: it reads DATAX_API_URL at
# module scope and raises if it is unset.
_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: E402
    DIAL_GLOBAL_BASE, fetch_dial_global_extractions)
from data_ingestion_engine.utils.ren.company_list import (  # noqa: E402
    load_flagged_pairs)

logger = logging.getLogger("snapshot_ren_baseline")

# The cohort currently being worked. Every script here takes --csv, so an
# earlier cohort stays runnable by naming it explicitly.
DEFAULT_CSV = ("data_ingestion_engine/"
               "Below 70 - below 70.csv")

REN_COMPANY_FIELD = "ren_company_id"


def parse_pairs(text: str) -> List[Dict[str, Any]]:
    """`25:2024,58:2026` -> cohort entries."""
    pairs = []
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        company_id, _, year = chunk.partition(":")
        pairs.append({"company_id": int(company_id), "year": int(year),
                      "company_name": None, "baseline_score": None})
    return pairs


def summarise(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The few fields worth having at a glance, without the 64-question body."""
    scoring = (payload or {}).get("scoring") or {}
    overall = scoring.get("overall_score") or {}
    documents = (payload or {}).get("source_documents") or []
    return {
        "total_score_percentage": overall.get("total_score_percentage"),
        "document_count": (payload or {}).get("document_count", len(documents)),
        "extraction_mode": (payload or {}).get("extraction_mode", "joint"),
        "source_documents": [
            {"report_id": d.get("report_id"),
             "document_type": d.get("document_type")}
            for d in documents],
        "category_scores": {
            name: (scoring.get("categories") or {}).get(name, {}).get("score")
            for name in ("culture", "inclusion", "talent", "impact", "equity",
                         "social")},
    }



# Companies queried at once when a bulk year read comes back empty.
YEAR_FALLBACK_WORKERS = int(os.getenv("SNAPSHOT_YEAR_FALLBACK_WORKERS", "12"))


def _rows_for_year_per_company(uid: str, year: int,
                               company_ids: List[int]) -> List[Dict[str, Any]]:
    """
    One year's rows, read a company at a time.

    A year-filtered read that returns nothing is not proof the year is empty.
    /extractions/ 500s under load — it already did so unfiltered, and as the
    table grew the year-filtered read started failing too: on 2026-08-28 the
    2025 read failed while 2026 returned 552 rows. Accepting the empty answer
    would silently drop a whole year from the snapshot, which is exactly the
    rollback someone would later reach for and find incomplete.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(company_id: int) -> List[Dict[str, Any]]:
        try:
            return fetch_dial_global_extractions(
                uid, company_id=company_id, year=year,
                company_field=REN_COMPANY_FIELD) or []
        except Exception as e:
            logger.warning("  company_id=%s year=%s failed: %s",
                           company_id, year, e)
            return []

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=YEAR_FALLBACK_WORKERS) as pool:
        for found in pool.map(one, company_ids):
            rows.extend(found)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Snapshot stored ReN rows before a progressive re-run.")
    parser.add_argument("--uid", required=True, help="user id for the internal JWT")
    parser.add_argument("--csv", default=DEFAULT_CSV,
                        help=f"scored export to read pairs from (default: {DEFAULT_CSV})")
    parser.add_argument("--max-score", type=float, default=70.0,
                        help="only snapshot rows at or below this score")
    parser.add_argument("--pairs", help="explicit pairs, e.g. 25:2024,58:2026")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")

    if args.pairs:
        pairs = parse_pairs(args.pairs)
        source = "--pairs"
    else:
        if not os.path.exists(args.csv):
            logger.error("Cohort file not found: %s", args.csv)
            return 2
        pairs, skipped = load_flagged_pairs(args.csv, max_score=args.max_score)
        source = args.csv
        logger.info("%s pair(s) from %s (%s row(s) above the score threshold)",
                    len(pairs), source, len(skipped))

    if not pairs:
        logger.error("No pairs to snapshot.")
        return 2

    logger.info("Reading from %s", DIAL_GLOBAL_BASE)

    wanted = {(p["company_id"], p["year"]) for p in pairs}
    years = sorted({p["year"] for p in pairs}, reverse=True)

    # One filtered read per distinct year, unioned on extraction_id. The
    # unfiltered read 500s on prod, so this is not an optimisation.
    by_key: Dict[tuple, Dict[str, Any]] = {}
    seen_extraction_ids = set()
    for year in years:
        try:
            rows = fetch_dial_global_extractions(
                args.uid, year=year, company_field=REN_COMPANY_FIELD)
        except Exception as e:
            logger.error("Read failed for year=%s: %s", year, e)
            return 1

        if not rows:
            logger.warning(
                "year=%s came back empty — re-reading per company to tell an "
                "empty year from a failed request", year)
            rows = _rows_for_year_per_company(
                uid=args.uid, year=year,
                company_ids=sorted({p["company_id"] for p in pairs}))
            logger.info("year=%s: %s row(s) recovered per-company",
                        year, len(rows))

        kept = 0
        for row in rows:
            company_id = row.get(REN_COMPANY_FIELD)
            if company_id is None:
                continue
            key = (int(company_id), int(row.get("year") or 0))
            if key not in wanted:
                continue
            extraction_id = row.get("extraction_id")
            if extraction_id in seen_extraction_ids:
                continue
            seen_extraction_ids.add(extraction_id)
            # Keep the newest row per company-year, matching how the export
            # resolves duplicates.
            incumbent = by_key.get(key)
            if incumbent is None or (extraction_id or 0) > (
                    incumbent.get("extraction_id") or 0):
                by_key[key] = row
            kept += 1
        logger.info("year=%s: %s row(s) returned, %s in cohort", year, len(rows), kept)

    missing = sorted(wanted - set(by_key))
    if missing:
        logger.warning(
            "%s of %s cohort pair(s) have NO stored row — they will be created "
            "rather than replaced by the re-run. Examples: %s",
            len(missing), len(wanted), missing[:10])

    names = {p["company_id"]: p["company_name"] for p in pairs if p["company_name"]}
    baseline_scores = {(p["company_id"], p["year"]): p["baseline_score"]
                       for p in pairs}

    records = []
    for (company_id, year), row in sorted(by_key.items()):
        payload = row.get("extracted_data") or {}
        records.append({
            "company_id": company_id,
            "year": year,
            "company_name": names.get(company_id),
            "extraction_id": row.get("extraction_id"),
            "status": row.get("status"),
            "updated_at": row.get("updated_at"),
            "source_report_ids": row.get("source_report_ids"),
            "cohort_score": baseline_scores.get((company_id, year)),
            "summary": summarise(payload),
            # The full stored payload, so this file can restore the row.
            "extracted_data": payload,
        })

    doc_counts = Counter(r["summary"]["document_count"] for r in records)
    scores = [r["summary"]["total_score_percentage"] for r in records
              if r["summary"]["total_score_percentage"] is not None]

    snapshot = {
        "meta": {
            "source": source,
            "max_score": args.max_score,
            "datax_base": DIAL_GLOBAL_BASE,
            "pairs_requested": len(wanted),
            "rows_found": len(records),
            "pairs_missing": len(missing),
            "missing_pairs": [list(m) for m in missing],
            "years": years,
        },
        "totals": {
            "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
            "document_count_distribution": dict(sorted(doc_counts.items())),
        },
        "records": records,
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, default=str)

    size_mb = os.path.getsize(args.out) / 1e6
    logger.info("Wrote %s (%s row(s), %.1f MB)", args.out, len(records), size_mb)
    logger.info("Baseline mean score: %s | documents per row: %s",
                snapshot["totals"]["mean_score"],
                snapshot["totals"]["document_count_distribution"])

    if len(records) < len(wanted):
        logger.warning(
            "Snapshot is INCOMPLETE (%s of %s). Rows with no baseline cannot be "
            "rolled back — decide whether that is acceptable before running.",
            len(records), len(wanted))

    return 0


if __name__ == "__main__":
    sys.exit(main())
