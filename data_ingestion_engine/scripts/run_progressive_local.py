#!/usr/bin/env python
"""
Run the progressive engine locally over a handful of company-years.

In-process: no Celery, no Redis, no web server. Documents and results still come
from whatever DATAX_API_URL points at, and the model calls are real.

WRITES ARE OFF BY DEFAULT. Without --store nothing is sent to DataX: the results
are captured in memory and written to a local JSON instead. That matters because
storage upserts on (ren_company_id, year) and REPLACES the existing scored row in
place — and this repo's .env currently points at the PROD backend, so an
unguarded local run is a production write.

Three levels, cheapest first:

  --plan      Resolve documents and print the rounds that WOULD run. GETs only,
              zero model calls, zero cost. Start here.
  (default)   Run the model for real, print the round traces, write local JSON.
              Costs model calls. Changes nothing in DataX.
  --store     Additionally persist each result, REPLACING the stored row.

Usage:
    # what would happen, for free
    python -m data_ingestion_engine.scripts.run_progressive_local \
        --uid <uid> --limit 5 --plan

    # really run 3 company-years against dev, keep the output local
    python -m data_ingestion_engine.scripts.run_progressive_local \
        --uid <uid> --api-url https://datax-backend-dev-...run.app \
        --limit 3 --out local_run.json

    # specific pairs
    python -m data_ingestion_engine.scripts.run_progressive_local \
        --uid <uid> --pairs 25:2024,58:2026 --out local_run.json
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

# --api-url has to win before store_data is imported: it reads DATAX_API_URL at
# module scope.
_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.tasks.company_year_engine import (  # noqa: E402
    EXTRACT_MODEL, SCORE_MODEL, run_company_year_extraction)
from data_ingestion_engine.utils.ren.company_group import (  # noqa: E402
    fetch_reports_for_company_group)
from data_ingestion_engine.utils.ren.company_list import (  # noqa: E402
    load_flagged_pairs)
from data_ingestion_engine.utils.ren.document_selection import (  # noqa: E402
    select_all_ren_documents)
from data_ingestion_engine.utils.ren.store_data import (  # noqa: E402
    store_ren_extraction)
from data_ingestion_engine.utils.store_data import DATAX_API_URL  # noqa: E402

logger = logging.getLogger("run_progressive_local")

# The cohort currently being worked. Every script here takes --csv, so an
# earlier cohort stays runnable by naming it explicitly.
DEFAULT_CSV = ("data_ingestion_engine/"
               "Below 70 - below 70.csv")

PROD_MARKERS = ("datax-backend-prod", "ingestion-engine.myrenx.ai")


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


def group_rounds(docs: List[Dict[str, Any]]) -> "list":
    """The rounds the engine will run, without running them."""
    from collections import OrderedDict
    groups: "OrderedDict[str, list]" = OrderedDict()
    for doc in docs:
        groups.setdefault(doc.get("document_type") or "Document", []).append(doc)
    return list(groups.items())


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Run the progressive engine locally. Writes are opt-in.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--max-score", type=float, default=70.0)
    parser.add_argument("--pairs", help="explicit pairs, e.g. 25:2024,58:2026")
    parser.add_argument("--baseline",
                        help="ren_baseline_*.json; preferred over --csv for deltas")
    parser.add_argument("--limit", type=int,
                        help="take the first N pairs from the cohort")
    parser.add_argument("--plan", action="store_true",
                        help="resolve documents and print the rounds; NO model calls")
    parser.add_argument("--store", action="store_true",
                        help="persist results, REPLACING the stored row")
    parser.add_argument("--out", default="local_progressive_run.json")
    parser.add_argument("--max-per-type", type=int)
    parser.add_argument("--max-per-year", type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")
    # The engine's own progress lines are the point of running this.
    logging.getLogger("data_ingestion_engine").setLevel(logging.INFO)

    # ---- cohort ----
    if args.pairs:
        pairs = parse_pairs(args.pairs)
        # Explicit pairs still deserve their baseline: without it every delta
        # reads as null and the run cannot answer the only question a pilot is
        # for — did the score move, and by how much.
        #
        # A snapshot beats the CSV when one is available: it holds what is
        # actually STORED right now, whereas the CSV is an export that may
        # predate later writes.
        if args.baseline and os.path.exists(args.baseline):
            with open(args.baseline, encoding="utf-8") as handle:
                snap = {(r["company_id"], r["year"]): r
                        for r in json.load(handle).get("records", [])}
            for pair in pairs:
                record = snap.get((pair["company_id"], pair["year"]))
                if record:
                    pair["baseline_score"] = (
                        record.get("summary") or {}).get("total_score_percentage")
                    pair["company_name"] = (pair["company_name"]
                                            or record.get("company_name"))
            logger.info("baselines from snapshot %s for %s/%s pair(s)",
                        args.baseline,
                        sum(1 for p in pairs if p["baseline_score"] is not None),
                        len(pairs))
        elif os.path.exists(args.csv):
            known = {(p["company_id"], p["year"]): p
                     for p in load_flagged_pairs(args.csv, max_score=1e9)[0]}
            for pair in pairs:
                match = known.get((pair["company_id"], pair["year"]))
                if match:
                    pair["baseline_score"] = match["baseline_score"]
                    pair["company_name"] = pair["company_name"] or match["company_name"]
            found = sum(1 for p in pairs if p["baseline_score"] is not None)
            logger.info("baselines found for %s/%s pair(s) in %s",
                        found, len(pairs), args.csv)
    else:
        if not os.path.exists(args.csv):
            logger.error("Cohort file not found: %s", args.csv)
            return 2
        pairs, _ = load_flagged_pairs(args.csv, max_score=args.max_score)
    if args.limit:
        pairs = pairs[:args.limit]
    if not pairs:
        logger.error("No company-years to run.")
        return 2

    # ---- where are we pointed, and is that safe? ----
    is_prod = any(marker in DATAX_API_URL for marker in PROD_MARKERS)
    banner = "PRODUCTION" if is_prod else "non-production"
    print(f"\n  DataX:   {DATAX_API_URL}  [{banner}]")
    print(f"  Bucket:  {os.getenv('GCS_BUCKET_NAME')}")
    print(f"  Models:  extract={EXTRACT_MODEL}  score={SCORE_MODEL}")
    print(f"  Pairs:   {len(pairs)}")
    print(f"  Mode:    {'PLAN (no model calls)' if args.plan else 'RUN'}"
          f"{'  + STORE' if args.store and not args.plan else ''}\n")

    if args.store and not args.plan:
        print("  !! --store REPLACES the stored row for each pair. DataX keeps")
        print("     no history; snapshot_ren_baseline.py is the only rollback.")
        if is_prod:
            print("  !! and this is PRODUCTION.")
        if input("     Type 'yes' to continue: ").strip().lower() != "yes":
            print("     Aborted.")
            return 1
        print()

    captured: List[Dict[str, Any]] = []

    def capturing_store(company_id, year, extracted_data, uid,
                        source_report_ids=None, status=None, **kwargs):
        """Stands in for store_ren_extraction when --store is off."""
        captured.append({
            "company_id": company_id, "year": year, "status": status,
            "source_report_ids": source_report_ids,
            "extracted_data": extracted_data,
        })
        # Shaped like a DataX response so the engine's success path runs
        # exactly as it would in production.
        return {"extraction_id": f"local-{company_id}-{year}", "created": True}

    store_fn = store_ren_extraction if args.store else capturing_store

    results: List[Dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        company_id, year = pair["company_id"], pair["year"]
        name = pair.get("company_name") or ""
        print(f"--- [{index}/{len(pairs)}] company_id={company_id} year={year} "
              f"{name}")

        try:
            payload = fetch_reports_for_company_group(company_id, args.uid)
            docs = select_all_ren_documents(
                payload, year=year,
                max_per_type=args.max_per_type, max_per_year=args.max_per_year)
        except Exception as e:
            logger.error("  document lookup failed: %s", e)
            results.append({"company_id": company_id, "year": year,
                            "error": f"document lookup failed: {e}"})
            continue

        rounds = group_rounds(docs)
        if not rounds:
            print("    no usable documents for this year — nothing to run")
            results.append({"company_id": company_id, "year": year,
                            "documents": 0, "rounds": 0,
                            "note": "no usable documents"})
            continue

        print(f"    {len(docs)} document(s) -> {len(rounds)} round(s):")
        for round_number, (document_type, group) in enumerate(rounds, start=1):
            ids = [d.get("report_id") for d in group]
            print(f"      round {round_number}: {document_type} "
                  f"({len(group)} doc(s), report_ids={ids})")

        if args.plan:
            results.append({
                "company_id": company_id, "year": year,
                "company_name": pair.get("company_name"),
                "baseline_score": pair.get("baseline_score"),
                "documents": len(docs), "rounds": len(rounds),
                "plan": [{"round": n, "document_type": t,
                          "report_ids": [d.get("report_id") for d in g]}
                         for n, (t, g) in enumerate(rounds, start=1)],
            })
            continue

        before = len(captured)
        outcome = run_company_year_extraction(
            task_id=f"local-{company_id}-{year}",
            company_id=company_id,
            uid=args.uid,
            docs=docs,
            store_fn=store_fn,
            label="ren-progressive-local",
            section_name="run_progressive_local",
            year=year,
            company_name=pair.get("company_name"),
            progressive=True,
        )

        for line in outcome.get("results", []):
            print(f"    OK  {line}")
        for line in outcome.get("errors", []):
            print(f"    ERR {line}")

        entry: Dict[str, Any] = {
            "company_id": company_id, "year": year,
            "company_name": pair.get("company_name"),
            "baseline_score": pair.get("baseline_score"),
            "documents": len(docs), "rounds_planned": len(rounds),
            "engine_results": outcome.get("results"),
            "engine_errors": outcome.get("errors"),
        }
        # Whatever the capturing store held on to this iteration.
        for record in captured[before:]:
            data = record["extracted_data"] or {}
            scoring = data.get("scoring") or {}
            entry.update({
                "status": record["status"],
                "score": (scoring.get("overall_score") or {}).get(
                    "total_score_percentage"),
                "questions_answered": data.get("questions_answered"),
                "rounds_run": data.get("rounds_run"),
                "rounds_failed": data.get("rounds_failed"),
                "extraction_rounds": data.get("extraction_rounds"),
                "answer_provenance": data.get("answer_provenance"),
                "extracted_data": data,
            })
            if entry["score"] is not None and pair.get("baseline_score") is not None:
                entry["delta"] = round(entry["score"] - pair["baseline_score"], 2)
        results.append(entry)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({
            "meta": {
                "datax": DATAX_API_URL, "is_production": is_prod,
                "mode": "plan" if args.plan else ("run+store" if args.store
                                                  else "run"),
                "stored_to_datax": bool(args.store and not args.plan),
                "pairs": len(pairs),
                "models": {"extract": EXTRACT_MODEL, "score": SCORE_MODEL},
            },
            "results": results,
        }, handle, indent=2, default=str)

    print(f"\nWrote {args.out}")

    if args.plan:
        total_rounds = sum(r.get("rounds", 0) for r in results)
        total_docs = sum(r.get("documents", 0) for r in results)
        print(f"\n  {total_docs} document(s) across {total_rounds} round(s)")
        print(f"  ~{total_rounds + len(results)} model calls if you run it "
              f"({total_rounds} extraction + {len(results)} scoring)")
    else:
        scored = [r for r in results if r.get("score") is not None]
        deltas = [r["delta"] for r in results if r.get("delta") is not None]
        print(f"\n  {len(scored)}/{len(results)} scored")
        if deltas:
            print(f"  mean delta vs baseline: "
                  f"{round(sum(deltas) / len(deltas), 2):+}")
            for r in results:
                if r.get("delta") is not None:
                    print(f"    {r['company_id']}/{r['year']}  "
                          f"{r['baseline_score']} -> {r['score']}  "
                          f"({r['delta']:+})  "
                          f"{r.get('questions_answered')}/64 answered, "
                          f"{r.get('rounds_run')} round(s)")
        if not args.store:
            print("\n  Nothing was written to DataX (--store was not passed).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
