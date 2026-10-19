#!/usr/bin/env python
"""
Build a baseline snapshot from a scored export CSV, when no snapshot was taken.

snapshot_ren_baseline.py reads the stored rows straight from DataX and is the
right tool — it captures each row's complete `extracted_data`, which is what
makes a rollback possible. Run it BEFORE a re-run.

This is the fallback for when that did not happen. A re-run replaces stored rows
in place and DataX keeps no history, so once it has finished the previous state
only survives in whatever export was taken beforehand. The cohort CSV is exactly
that: it carries each company-year's pre-run total, its category levels and its
document count.

What you get and what you do not:

  YES  total_score_percentage, the six category rubric levels, document_count,
       source document types — everything the before/after comparison needs.
  NO   the 64-question extraction body. So `questions_answered` has nothing to
       compare against and is reported as null rather than zero, and this file
       CANNOT be used to restore rows with restore_ren_baseline.py.

Usage:
    python -m data_ingestion_engine.scripts.baseline_from_csv \
        --csv "data_ingestion_engine/Below 70 - below 70.csv" \
        --out ren_baseline_below70.json
"""

import argparse
import csv as _csv
import json
import logging
import os
import statistics
import sys
from collections import Counter
from typing import Any, Dict, Optional

logger = logging.getLogger("baseline_from_csv")

CATEGORIES = ("culture", "inclusion", "talent", "impact", "equity", "social")


def _num(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct a baseline snapshot from a scored export CSV.")
    parser.add_argument("--csv", required=True, help="the pre-run scored export")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-score", type=float, default=70.0)
    parser.add_argument("--universe", default="ReN")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.csv):
        logger.error("Not found: %s", args.csv)
        return 2

    records = []
    skipped = Counter()
    with open(args.csv, newline="", encoding="utf-8-sig") as handle:
        for row in _csv.DictReader(handle):
            row = {str(k).strip(): v for k, v in row.items()}

            if args.universe and (row.get("Universe") or "").strip() != args.universe:
                skipped["wrong universe"] += 1
                continue

            company_id = _int(row.get("Company ID"))
            year = _int(row.get("Year"))
            score = _num(row.get("total_score_percentage"))
            if company_id is None or year is None or score is None:
                skipped["unparseable id/year/score"] += 1
                continue
            if score > args.max_score:
                skipped["above the score threshold"] += 1
                continue

            categories = {}
            for name in CATEGORIES:
                level = _int(row.get(f"{name}_score"))
                if level is None:
                    continue
                categories[name] = {
                    "score": level,
                    "category_weight_percent": _num(row.get(f"{name}_weight")),
                    "weighted_score": _num(row.get(f"{name}_weighted_score")),
                    "score_rationale": row.get(f"{name}_score_rationale") or "",
                    "uncertainty_flagged": str(
                        row.get(f"{name}_uncertainty_flagged", "")).strip().upper() == "TRUE",
                    "problematic_questions": [
                        q.strip() for q in
                        (row.get(f"{name}_problematic_questions") or "").split(",")
                        if q.strip()],
                }

            documents = [
                {"report_id": None, "document_type": t.strip()}
                for t in (row.get("Source Documents") or "").split(",") if t.strip()]
            document_count = _int(row.get("Documents")) or len(documents)

            # Shaped exactly like a stored row's extracted_data, minus the
            # `extraction` key — which the CSV does not carry and which nothing
            # downstream should pretend to have.
            payload = {
                "reporting_year": year,
                "document_count": document_count,
                "source_documents": documents,
                "extraction_mode": "joint",
                "scoring": {
                    "organisation_name": row.get("Company"),
                    "year": year,
                    "categories": categories,
                    "overall_score": {"total_score_percentage": score},
                },
                "scoring_consensus": None,
            }

            records.append({
                "company_id": company_id,
                "year": year,
                "company_name": row.get("Company"),
                "extraction_id": _int(row.get("Extraction ID")),
                "status": "scored",
                "updated_at": "",
                "source_report_ids": [],
                "cohort_score": score,
                "summary": {
                    "total_score_percentage": score,
                    "document_count": document_count,
                    "extraction_mode": "joint",
                    "source_documents": documents,
                    "category_scores": {n: categories.get(n, {}).get("score")
                                        for n in CATEGORIES},
                },
                "extracted_data": payload,
            })

    if not records:
        logger.error("No usable rows in %s", args.csv)
        return 2

    scores = [r["summary"]["total_score_percentage"] for r in records]
    snapshot = {
        "meta": {
            "source": args.csv,
            "reconstructed_from_csv": True,
            "max_score": args.max_score,
            "pairs_requested": len(records),
            "rows_found": len(records),
            "pairs_missing": 0,
            "note": ("Rebuilt from a scored export, not read from DataX. Carries "
                     "scores and category levels but NOT the 64-question "
                     "extraction bodies, so it cannot restore rows."),
        },
        "totals": {
            "mean_score": round(statistics.mean(scores), 2),
            "document_count_distribution": dict(sorted(
                Counter(r["summary"]["document_count"] for r in records).items())),
        },
        "records": records,
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, default=str)

    logger.info("Wrote %s (%s row(s), %.1f MB)", args.out, len(records),
                os.path.getsize(args.out) / 1e6)
    logger.info("  mean score %.2f | documents %s",
                snapshot["totals"]["mean_score"],
                snapshot["totals"]["document_count_distribution"])
    if skipped:
        logger.info("  skipped: %s", dict(skipped))
    logger.warning(
        "This baseline CANNOT restore rows — it has no extraction bodies. "
        "Use snapshot_ren_baseline.py BEFORE the next re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
