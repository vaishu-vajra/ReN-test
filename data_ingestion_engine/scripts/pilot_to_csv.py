#!/usr/bin/env python
"""
Turn a pilot run into the sheet you already read.

run_progressive_local.py writes JSON with round traces in it. That is the right
shape for debugging the engine and the wrong shape for judging the results. This
converts it into the same columns as the Scoring sheet of dial_global_final.xlsx
— same headers, same order — with the before/after columns added on the front so
the comparison is the first thing you see.

Reuses build_scoring_rows() and build_all_rows() from
utils/dial_global/export.py, so a column that appears in the workbook appears
here identically and cannot drift.

Writes two files:

  <out>.csv        Scoring sheet + before/after columns. The one to read.
  <out>_all.csv    the wide sheet, all 64 questions with justifications
                   (--all only; it is large)

Usage:
    python -m data_ingestion_engine.scripts.pilot_to_csv \
        --pilot pilot.json --baseline ren_baseline_2026-08-25.json \
        --out pilot_results

    # add the wide per-question sheet
    ... --all

    # a real .xlsx instead of CSVs
    ... --xlsx
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")

import django  # noqa: E402

django.setup()

import pandas as pd  # noqa: E402

from data_ingestion_engine.utils.dial_global.export import (  # noqa: E402
    ALL_COLUMNS, SCORING_COLUMNS, build_all_rows, build_scoring_rows)

logger = logging.getLogger("pilot_to_csv")

# Put in front of the workbook's own columns: the pilot's whole purpose is the
# comparison, so it should not be buried 40 columns to the right.
COMPARISON_COLUMNS = [
    "Baseline Score", "New Score", "Score Delta",
    "Baseline Documents", "New Documents",
    "Rounds", "Rounds Failed", "Round Order",
    "Questions Answered", "Baseline Questions", "Questions Delta",
]


def as_records(pilot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Shape the pilot's captured results like DataX extraction rows.

    build_scoring_rows() expects what the API returns — a row carrying
    `extracted_data`, `ren_company_id` and `extraction_id`. The local runner
    holds the same payload under a different shape, so this adapts rather than
    duplicating the column logic.
    """
    records = []
    for entry in pilot.get("results", []):
        payload = entry.get("extracted_data")
        if not payload:
            # Nothing was extracted — no documents, or every round failed.
            continue
        records.append({
            "ren_company_id": entry["company_id"],
            "year": entry["year"],
            "extraction_id": f"pilot-{entry['company_id']}-{entry['year']}",
            "status": entry.get("status"),
            "updated_at": "",
            "extracted_data": payload,
        })
    return records


def answered_count(payload: Optional[Dict[str, Any]]) -> Optional[int]:
    """How many of the 64 questions carry a score."""
    if not payload:
        return None
    stored = payload.get("questions_answered")
    if stored is not None:
        return int(stored)
    extraction = payload.get("extraction") or {}
    return sum(
        1 for section in extraction.values() if isinstance(section, dict)
        for value in section.values()
        if isinstance(value, dict) and value.get("score") == 1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a pilot run into the familiar Scoring sheet.")
    parser.add_argument("--pilot", default="pilot.json")
    parser.add_argument("--baseline",
                        help="ren_baseline_*.json, for the before/after columns")
    parser.add_argument("--out", default="pilot_results")
    parser.add_argument("--all", action="store_true",
                        help="also write the wide 64-question sheet")
    parser.add_argument("--xlsx", action="store_true",
                        help="write one .xlsx with both sheets instead of CSVs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.pilot):
        logger.error("Pilot file not found: %s", args.pilot)
        return 2
    with open(args.pilot, encoding="utf-8") as handle:
        pilot = json.load(handle)

    baseline: Dict[tuple, Dict[str, Any]] = {}
    if args.baseline:
        if not os.path.exists(args.baseline):
            logger.error("Baseline not found: %s", args.baseline)
            return 2
        with open(args.baseline, encoding="utf-8") as handle:
            for record in json.load(handle).get("records", []):
                baseline[(record["company_id"], record["year"])] = record

    # Names come from the pilot, keyed by company id, so the sheet is not named
    # after whatever the model read off each document.
    names = {entry["company_id"]: entry.get("company_name")
             for entry in pilot.get("results", []) if entry.get("company_name")}

    records = as_records(pilot)
    if not records:
        logger.error("No extractions in %s — nothing to write.", args.pilot)
        return 2

    rows = build_scoring_rows(records, ren_company_names=names)

    # Index the pilot by (company, year) so the comparison columns can be
    # attached to the rows build_scoring_rows produced.
    by_key = {(e["company_id"], e["year"]): e for e in pilot.get("results", [])}

    for row in rows:
        key = (row["Company ID"], row["Year"])
        entry = by_key.get(key, {})
        base = baseline.get(key)
        base_payload = (base or {}).get("extracted_data") or {}
        base_summary = (base or {}).get("summary") or {}

        base_score = base_summary.get("total_score_percentage")
        new_score = row.get("total_score_percentage")
        base_answered = answered_count(base_payload) if base else None
        new_answered = entry.get("questions_answered")

        rounds = entry.get("extraction_rounds") or []
        order = []
        for record in rounds:
            document_type = record.get("document_type")
            if document_type and document_type not in order:
                order.append(document_type)

        row.update({
            "Baseline Score": base_score,
            "New Score": new_score,
            "Score Delta": (round(new_score - base_score, 2)
                            if None not in (base_score, new_score) else None),
            "Baseline Documents": base_summary.get("document_count"),
            "New Documents": entry.get("documents"),
            "Rounds": entry.get("rounds_run"),
            "Rounds Failed": entry.get("rounds_failed"),
            "Round Order": " -> ".join(order),
            "Questions Answered": new_answered,
            "Baseline Questions": base_answered,
            "Questions Delta": (new_answered - base_answered
                                if None not in (new_answered, base_answered)
                                else None),
        })
        # The runner's ids are placeholders; the sheet's column is for real ones.
        row["Extraction ID"] = None
        if not row.get("Company"):
            row["Company"] = names.get(row["Company ID"])

    columns = COMPARISON_COLUMNS + SCORING_COLUMNS
    scoring = pd.DataFrame(rows).reindex(
        columns=["Company", "Year"]
        + [c for c in columns if c not in ("Company", "Year")])
    scoring = scoring.sort_values("Score Delta", ascending=False,
                                  na_position="last")

    frames = {"Scoring": scoring}
    if args.all:
        frames["All"] = pd.DataFrame(
            build_all_rows(records, ren_company_names=names)
        ).reindex(columns=ALL_COLUMNS)

    if args.xlsx:
        path = f"{args.out}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for sheet, frame in frames.items():
                frame.to_excel(writer, sheet_name=sheet, index=False)
        written = [path]
    else:
        written = []
        for sheet, frame in frames.items():
            suffix = "" if sheet == "Scoring" else "_all"
            path = f"{args.out}{suffix}.csv"
            frame.to_csv(path, index=False)
            written.append(path)

    for path in written:
        logger.info("Wrote %s (%.0f KB)", path, os.path.getsize(path) / 1024)

    # A short read-out, so the file does not have to be opened to know the answer.
    deltas = scoring["Score Delta"].dropna()
    print(f"\n{'company':<30} {'base':>6} {'new':>6} {'delta':>7} {'docs':>5} "
          f"{'rnds':>5}  round order")
    print("-" * 108)
    for _, row in scoring.iterrows():
        print(f"{str(row['Company'])[:30]:<30} "
              f"{row['Baseline Score']!s:>6} {row['New Score']!s:>6} "
              f"{row['Score Delta']!s:>7} {row['New Documents']!s:>5} "
              f"{row['Rounds']!s:>5}  {str(row['Round Order'])[:44]}")
    if len(deltas):
        print("-" * 108)
        print(f"  mean {deltas.mean():+.1f}   median {deltas.median():+.1f}   "
              f"improved {int((deltas > 0).sum())}  "
              f"flat {int((deltas == 0).sum())}  "
              f"regressed {int((deltas < 0).sum())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
