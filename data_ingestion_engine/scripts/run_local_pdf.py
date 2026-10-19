#!/usr/bin/env python
"""
Run the progressive engine over PDFs on local disk, for one company-year.

Built for repaired documents. A PDF that Gemini refuses — over its ~50 MB
per-file limit, and beyond what Ghostscript can shrink — costs its company-year
entirely when it is that year's only evidence. Once someone has compressed the
file by hand it needs to be re-read, but it is not in GCS yet and re-ingesting
just to test it is a slow loop.

This reads the local file directly and runs the real engine over it, so the
result is exactly what a re-ingested document would produce.

WRITES ARE OFF BY DEFAULT. Without --store nothing is sent to DataX; the result
goes to a workbook and a JSON beside it.

Usage:
    python -m data_ingestion_engine.scripts.run_local_pdf \
        --uid <uid> --universe dg --company-id 3499 --year 2025 \
        --pdf "sahinbinden ...compressed.pdf::Sustainability Report::3515" \
        --out sahibinden_2025

--pdf is repeatable and takes  path::document_type::report_id  — report_id is
optional and only used to label provenance.
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

import pandas as pd  # noqa: E402

import data_ingestion_engine.tasks.company_year_engine as engine  # noqa: E402
from data_ingestion_engine.tasks.dial_global.extract import (  # noqa: E402
    order_for_progressive)
from data_ingestion_engine.utils.dial_global.export import (  # noqa: E402
    _strip_illegal, build_workbook, write_leaderboard)
from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: E402
    store_dial_global_extraction)
from data_ingestion_engine.utils.ren.store_data import (  # noqa: E402
    store_ren_extraction)

logger = logging.getLogger("run_local_pdf")

_ORIGINAL_LOAD = engine.load_pdf_bytes


def _load_local_or_remote(document_url: str) -> bytes:
    """
    Serve a local path straight off disk, anything else as usual.

    Swapped in for engine.load_pdf_bytes so the rest of the engine — grouping,
    rounds, the residual catalogue, scoring — runs completely unmodified. Only
    where the bytes come from changes.
    """
    if os.path.exists(document_url):
        with open(document_url, "rb") as handle:
            return handle.read()
    return _ORIGINAL_LOAD(document_url)


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Progressive extraction over local PDFs for one company-year.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--universe", choices=("dg", "ren"), required=True)
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--company-name")
    parser.add_argument("--pdf", action="append", required=True,
                        help="path::document_type[::report_id], repeatable")
    parser.add_argument("--out", required=True, help="output stem, no extension")
    parser.add_argument("--store", action="store_true",
                        help="also persist the result to DataX")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("data_ingestion_engine").setLevel(logging.INFO)

    docs: List[Dict[str, Any]] = []
    for spec in args.pdf:
        parts = spec.split("::")
        path = parts[0]
        document_type = parts[1] if len(parts) > 1 else "Annual Report"
        report_id = int(parts[2]) if len(parts) > 2 and parts[2] else None
        if not os.path.exists(path):
            logger.error("Not found: %s", path)
            return 2
        docs.append({"report_id": report_id, "document_type": document_type,
                     "year": args.year, "document_url": path})
        logger.info("%s  %s  %.1f MB", document_type, path,
                    os.path.getsize(path) / 1e6)

    docs = order_for_progressive(docs)

    captured: List[Dict[str, Any]] = []

    def capture(company_id, year, extracted_data, uid,
                source_report_ids=None, status=None, **kwargs):
        captured.append({"company_id": company_id, "year": year,
                         "status": status, "extracted_data": extracted_data})
        if args.store:
            store = (store_dial_global_extraction if args.universe == "dg"
                     else store_ren_extraction)
            return store(company_id=company_id, year=year,
                         extracted_data=extracted_data, uid=uid,
                         source_report_ids=source_report_ids, status=status)
        return {"extraction_id": f"local-{company_id}-{year}", "created": True}

    engine.load_pdf_bytes = _load_local_or_remote
    try:
        outcome = engine.run_company_year_extraction(
            task_id=f"local-{args.company_id}-{args.year}",
            company_id=args.company_id, uid=args.uid, docs=docs,
            store_fn=capture, label=f"{args.universe}-local",
            section_name="run_local_pdf", year=args.year,
            company_name=args.company_name, progressive=True)
    finally:
        engine.load_pdf_bytes = _ORIGINAL_LOAD

    for line in outcome.get("results", []):
        print(f"  OK  {line}")
    for line in outcome.get("errors", []):
        print(f"  ERR {line}")

    if not captured:
        logger.error("Nothing extracted — no workbook written.")
        return 1

    field = "company_id" if args.universe == "dg" else "ren_company_id"
    records = [{field: r["company_id"], "year": r["year"],
                "extraction_id": None, "status": r["status"], "updated_at": "",
                "extracted_data": r["extracted_data"]} for r in captured]
    names = ({args.company_id: args.company_name} if args.company_name else None)
    frames = build_workbook(
        records,
        company_names=names if args.universe == "dg" else None,
        ren_company_names=names if args.universe == "ren" else None)
    for name, frame in frames.items():
        frames[name] = _strip_illegal(frame)

    write_leaderboard(frames, f"{args.out}.xlsx")
    with open(f"{args.out}.json", "w", encoding="utf-8") as handle:
        json.dump(captured, handle, indent=2, default=str)

    print(f"\n  Wrote {args.out}.xlsx and {args.out}.json")
    if not args.store:
        print("  Nothing written to DataX (--store was not passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
