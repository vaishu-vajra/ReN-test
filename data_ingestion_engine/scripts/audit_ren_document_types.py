#!/usr/bin/env python
"""
What the progressive run would actually read, without spending a model call.

Answers the three questions that decide whether the run is worth dispatching:

  1. Do the eleven type patterns hold against PROD values? The patterns were
     written against a 161-row ingestion mastersheet. This applies them to every
     document the cohort's companies hold and reports what classified as what —
     and, more importantly, what classified as NOTHING.

  2. How many documents does a company-year actually hold? That number is the
     round count, which is the model-call count, which is the cost and the wall
     clock. It also sets REN_MAX_DOCS_PER_YEAR.

  3. How much does the expansion gain? Every company-year is classified twice,
     under the old four-type rule and the new eleven-type one, so the delta is
     measured rather than assumed.

Read-only: GETs document metadata, writes two local files. Never writes to DataX
and never calls Gemini.

Usage:
    python -m data_ingestion_engine.scripts.audit_ren_document_types \
        --uid <uid> \
        --api-url https://<prod-datax-host> \
        --out-csv ren_type_audit.csv \
        --out-json ren_type_audit.json

Read the `unclassified` section of the JSON before running anything. It is the
only evidence that the patterns hold against real data.
"""

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.ren.company_list import (  # noqa: E402
    load_flagged_pairs)
from data_ingestion_engine.utils.ren.document_selection import (  # noqa: E402
    REN_ANCHOR_TYPES,
    REN_ROUND_ORDER,
    canonical_report_type,
    classify_document,
    select_all_documents_for_year,
    select_documents_for_year,
)
from data_ingestion_engine.utils.store_data import (  # noqa: E402
    fetch_document_metadata)

logger = logging.getLogger("audit_ren_document_types")

# The cohort currently being worked. Every script here takes --csv, so an
# earlier cohort stays runnable by naming it explicitly.
DEFAULT_CSV = ("data_ingestion_engine/"
               "Below 70 - below 70.csv")

# Fields the audit reads off a metadata row. If document_title is absent from
# every row the endpoint does not return it, and the title fallback in
# classify_document() is inert — which the summary reports explicitly.
METADATA_FIELDS = ("report_id", "company_id", "year", "report_type",
                   "document_title", "document_type", "document_url")


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Classify a cohort's documents offline; no model calls.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--max-score", type=float, default=70.0)
    parser.add_argument("--out-csv", default="ren_type_audit.csv")
    parser.add_argument("--out-json", default="ren_type_audit.json")
    parser.add_argument("--batch", type=int, default=200,
                        help="company ids per metadata request (server cap 200)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.csv):
        logger.error("Cohort file not found: %s", args.csv)
        return 2

    pairs, _skipped = load_flagged_pairs(args.csv, max_score=args.max_score)
    if not pairs:
        logger.error("No pairs in cohort.")
        return 2

    wanted = {(p["company_id"], p["year"]) for p in pairs}
    company_ids = sorted({p["company_id"] for p in pairs})
    names = {p["company_id"]: p["company_name"] for p in pairs}
    logger.info("%s company-year(s) across %s company/companies",
                len(wanted), len(company_ids))

    rows: List[Dict[str, Any]] = []
    for offset in range(0, len(company_ids), args.batch):
        chunk = company_ids[offset:offset + args.batch]
        found = fetch_document_metadata(chunk, args.uid)
        if found is None:
            logger.error(
                "Document metadata endpoint unavailable. This audit needs it — "
                "the per-company fallback would be %s separate reads.",
                len(company_ids))
            return 1
        rows.extend(found)
        logger.info("  %s/%s companies -> %s document(s) so far",
                    min(offset + args.batch, len(company_ids)),
                    len(company_ids), len(rows))

    logger.info("%s document row(s) fetched", len(rows))

    has_title = sum(1 for r in rows if r.get("document_title"))
    if not has_title:
        logger.warning(
            "NO row carries document_title — the title fallback in "
            "classify_document() is inert against this endpoint. Documents "
            "typed 'Others' will stay unclassified.")

    # ---- classify every document ----
    classified: List[Dict[str, Any]] = []
    label_counts = Counter()
    unclassified = Counter()
    source_field = Counter()

    for row in rows:
        label = classify_document(row)
        label_counts[label or "(unclassified)"] += 1

        if label is None:
            unclassified[(str(row.get("report_type") or "").strip(),
                          str(row.get("document_title") or "").strip()[:60])] += 1
        else:
            # Which field actually produced the answer, so the value of the
            # title fallback is measurable rather than assumed.
            for field in ("report_type", "document_title", "document_type"):
                if canonical_report_type(row.get(field)):
                    source_field[field] += 1
                    break

        classified.append({
            **{f: row.get(f) for f in METADATA_FIELDS},
            "canonical_type": label,
            "company_name": names.get(row.get("company_id")),
        })

    # ---- per company-year: old rule vs new rule ----
    by_company_year: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in classified:
        if not row["canonical_type"] or not row.get("document_url"):
            continue
        try:
            year = int(row["year"])
        except (TypeError, ValueError):
            continue
        by_company_year[(int(row["company_id"]), year)].append({
            "report_id": row["report_id"],
            "document_type": row["canonical_type"],
            "year": year,
            "document_url": row["document_url"],
        })

    old_counts, new_counts, round_counts = Counter(), Counter(), Counter()
    rounds_by_type = Counter()
    total_rounds = 0
    per_year_detail = []
    gained = 0

    for key in sorted(wanted):
        docs = by_company_year.get(key, [])
        old = select_documents_for_year(docs)
        new = select_all_documents_for_year(docs)

        # A ROUND is one report type, not one document — every document of a
        # type goes into a single call. Counting documents here would inflate
        # the model-call estimate for exactly the companies the change helps
        # most: the ones filing several documents of one type.
        types_present = []
        for doc in new:
            if doc["document_type"] not in types_present:
                types_present.append(doc["document_type"])
        rounds = len(types_present)

        old_counts[len(old)] += 1
        new_counts[len(new)] += 1
        round_counts[rounds] += 1
        total_rounds += rounds
        for document_type in types_present:
            rounds_by_type[document_type] += 1

        if len(new) > len(old):
            gained += 1
        per_year_detail.append({
            "company_id": key[0], "year": key[1],
            "company_name": names.get(key[0]),
            "old_rule_documents": len(old),
            "new_rule_documents": len(new),
            "new_rule_rounds": rounds,
            "old_types": [d["document_type"] for d in old],
            "new_types": [d["document_type"] for d in new],
            "round_order": types_present,
        })

    # One scoring call per company-year that has anything to extract.
    scoring_calls = sum(1 for d in per_year_detail if d["new_rule_documents"] > 0)
    model_calls = total_rounds + scoring_calls

    summary = {
        "cohort": {
            "source": args.csv, "max_score": args.max_score,
            "company_years": len(wanted), "companies": len(company_ids),
        },
        "documents": {
            "rows_fetched": len(rows),
            "rows_with_document_title": has_title,
            "classified_by_field": dict(source_field),
            "by_canonical_type": {k: label_counts[k] for k in
                                  list(REN_ROUND_ORDER) + ["(unclassified)"]
                                  if k in label_counts},
        },
        "unclassified": [
            {"report_type": rt, "document_title": dt, "count": n}
            for (rt, dt), n in unclassified.most_common(200)],
        "documents_per_company_year": {
            "old_rule": dict(sorted(old_counts.items())),
            "new_rule": dict(sorted(new_counts.items())),
        },
        # Rounds, not documents — this is the one that drives cost and latency.
        "rounds_per_company_year": dict(sorted(round_counts.items())),
        "rounds_by_report_type": dict(rounds_by_type.most_common()),
        "impact": {
            "company_years_gaining_documents": gained,
            "company_years_still_empty": new_counts.get(0, 0),
            "total_extraction_rounds": total_rounds,
            "total_scoring_calls": scoring_calls,
            "estimated_model_calls": model_calls,
            "max_documents_in_one_year": max(new_counts) if new_counts else 0,
            "max_rounds_in_one_year": max(round_counts) if round_counts else 0,
            "old_rule_anchor_types": REN_ANCHOR_TYPES,
        },
        "company_years": per_year_detail,
    }

    with open(args.out_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(METADATA_FIELDS) + ["canonical_type",
                                                        "company_name"])
        writer.writeheader()
        writer.writerows(classified)

    logger.info("Wrote %s and %s", args.out_json, args.out_csv)
    print("\n--- classification ---")
    for label, count in label_counts.most_common():
        print(f"  {count:6}  {label}")
    print("\n--- documents per company-year ---")
    print(f"  old rule (4 types):  {dict(sorted(old_counts.items()))}")
    print(f"  new rule (11 types): {dict(sorted(new_counts.items()))}")
    print("\n--- ROUNDS per company-year (one round = one report type) ---")
    print(f"  {dict(sorted(round_counts.items()))}")
    print("\n--- rounds each report type will run ---")
    for document_type, count in rounds_by_type.most_common():
        print(f"  {count:6}  {document_type}")
    print(f"\n  {gained} company-year(s) gain documents")
    print(f"  {new_counts.get(0, 0)} company-year(s) still have none")
    print(f"  {total_rounds} extraction rounds + {scoring_calls} scoring calls "
          f"= ~{model_calls} model calls")
    print(f"  largest company-year: {max(new_counts) if new_counts else 0} documents "
          f"in {max(round_counts) if round_counts else 0} rounds")
    print(f"  <- set REN_MAX_DOCS_PER_YEAR from the document figure, "
          f"DIAL_GLOBAL_MAX_DOCS_PER_ROUND from the busiest single type")
    if unclassified:
        print(f"\n--- top unclassified ({sum(unclassified.values())} documents) ---")
        for (rt, dt), n in unclassified.most_common(15):
            print(f"  {n:5}  report_type={rt!r} title={dt!r}")
        print("\n  ^ REVIEW THIS before dispatching. Anything here is evidence "
              "the run will not read.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
