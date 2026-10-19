#!/usr/bin/env python
"""
Export the DIAL Global scoring results to a leaderboard spreadsheet.

One row per company-year: a company with 2023 and 2024 documents gets two rows.

Usage:
    python -m data_ingestion_engine.scripts.export_dial_global_leaderboard \
        --uid <uid> --out dial_global_leaderboard.xlsx

    # from local Scoring_Output JSON files instead of DataX
    python -m data_ingestion_engine.scripts.export_dial_global_leaderboard \
        --from-json out/*.json --out leaderboard.csv

The DataX path reads every row of dial_global_extractions; the --from-json path
accepts either raw DataX rows or bare payloads of the shape the engine stores
({reporting_year, source_documents, extraction, scoring}).
"""

import argparse
import glob
import json
import logging
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.dial_global.export import (  # noqa: E402
    build_workbook,
    fetch_workbook,
    write_leaderboard,
)

logger = logging.getLogger(__name__)


def _load_json_records(patterns):
    """Read scoring output files into DataX-row shape."""
    records = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            items = data if isinstance(data, list) else [data]
            for item in items:
                # Already a DataX row.
                if isinstance(item, dict) and "extracted_data" in item:
                    records.append(item)
                    continue
                # A bare stored payload — wrap it so the flattener sees the
                # same shape either way.
                records.append({
                    "extraction_id": None,
                    "company_id": None,
                    "status": "scored",
                    "extracted_data": item,
                })
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="dial_global_leaderboard.xlsx",
        help="Output path. .xlsx or .csv, chosen by extension.")
    parser.add_argument(
        "--uid", help="User id for the internal JWT. Required unless --from-json is used.")
    parser.add_argument(
        "--from-json", nargs="+", metavar="GLOB",
        help="Read scoring output from local JSON files instead of DataX.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.from_json:
        workbook = build_workbook(_load_json_records(args.from_json))
    else:
        if not args.uid:
            parser.error("--uid is required unless --from-json is used")
        workbook = fetch_workbook(args.uid)

    if all(frame.empty for frame in workbook.values()):
        logger.warning("No scored DIAL Global company-years found — "
                       "writing empty sheets with headers only.")

    write_leaderboard(workbook, args.out)
    print(f"{len(workbook['Scoring'])} company-year row(s) -> {args.out}")
    for name, frame in workbook.items():
        print(f"  {name}: {len(frame)} rows x {len(frame.columns)} columns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
