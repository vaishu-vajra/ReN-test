#!/usr/bin/env python
"""
Find dial_global_companies ids for a list of company names. No model calls.

The DIAL Global progressive endpoint takes ids, and a tracking sheet carries
names. Ingestion creates the companies, so this is run AFTER ingestion — its
other job is confirming that ingestion actually landed: a company here with zero
documents means its rows failed to download, which for a sheet of Google Drive
links is the likely failure rather than an unlikely one.

Matching reuses the name comparison from resolve_company_cohort — legal suffixes
and punctuation ignored, identifying tokens compared exactly — so "Mercedes-Benz
Türk" matches "Mercedes-Benz Türk A.Ş." but not "Mercedes-Benz Group AG".

Read-only.

Usage:
    python -m data_ingestion_engine.scripts.dg_company_ids \
        --uid <uid> --api-url https://<prod-datax-host> \
        --csv "Turkish Companies Document Tracking DIAL Global - ....csv" \
        --out dg_turkish_ids.json
"""

import argparse
import csv as _csv
import json
import logging
import os
import sys
from collections import Counter
from typing import Any, Dict, List

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.scripts.resolve_company_cohort import (  # noqa: E402
    _name_matches)
from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: E402
    fetch_dial_global_companies, fetch_dial_global_reports_for_company)

logger = logging.getLogger("dg_company_ids")


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Map company names to dial_global_companies ids.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--name-column", default="Company Name")
    parser.add_argument("--out", default="dg_company_ids.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.csv):
        logger.error("Not found: %s", args.csv)
        return 2

    with open(args.csv, newline="", encoding="utf-8-sig") as handle:
        rows = [{str(k).strip(): v for k, v in r.items()}
                for r in _csv.DictReader(handle)]
    wanted = sorted({(r.get(args.name_column) or "").strip()
                     for r in rows if (r.get(args.name_column) or "").strip()})
    logger.info("%s distinct company name(s) in %s", len(wanted), args.csv)

    companies = fetch_dial_global_companies(args.uid) or []
    logger.info("%s company/companies in dial_global_companies", len(companies))

    results: List[Dict[str, Any]] = []
    for name in wanted:
        match = None
        for candidate in companies:
            stored = candidate.get("company_name") or candidate.get("name")
            if _name_matches(name, stored):
                match = candidate
                break

        if not match:
            results.append({"company": name, "company_id": None,
                            "matched_name": None, "documents": 0,
                            "status": "not_in_dial_global",
                            "detail": "no dial_global_companies row — has "
                                      "ingestion run for this company?"})
            continue

        company_id = int(match.get("company_id"))
        try:
            docs = fetch_dial_global_reports_for_company(company_id, args.uid) or []
        except Exception as e:
            logger.warning("  %s: document read failed — %s", name, e)
            docs = []
        docs = [d for d in docs if d.get("document_url")]

        by_year = Counter(d.get("year") for d in docs)
        by_type = Counter(d.get("document_type") for d in docs)
        results.append({
            "company": name,
            "company_id": company_id,
            "matched_name": match.get("company_name") or match.get("name"),
            "documents": len(docs),
            "years": dict(sorted((y, n) for y, n in by_year.items() if y)),
            "types": dict(by_type),
            "status": "ready" if docs else "no_documents",
            "detail": "" if docs else
                      "company exists but holds no documents — ingestion "
                      "either has not run or the downloads failed",
        })

    ready = [r for r in results if r["status"] == "ready"]
    counts = Counter(r["status"] for r in results)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"meta": {"source": args.csv, "total": len(results),
                            **dict(counts)},
                   "company_ids": [r["company_id"] for r in ready],
                   "results": results}, handle, indent=2, default=str)

    print(f"\n  ready               {counts.get('ready', 0)}")
    print(f"  no documents        {counts.get('no_documents', 0)}")
    print(f"  not in dial_global  {counts.get('not_in_dial_global', 0)}")
    print(f"\n  documents found: {sum(r['documents'] for r in ready)}")
    for record in results:
        if record["status"] != "ready":
            print(f"    {record['status']:<20} {record['company']}")
    print(f"\n  Wrote {args.out} — `company_ids` is what the endpoint takes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
