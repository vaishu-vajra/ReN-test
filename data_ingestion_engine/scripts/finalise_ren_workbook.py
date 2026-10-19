#!/usr/bin/env python
"""
Decide which score to PUBLISH per company-year, and write the workbook from it.

A re-run can move a score down for two very different reasons, and only one of
them is a finding:

  * The re-run read MORE documents and the fuller evidence scored lower. That is
    a real re-reading and it stands.
  * The re-run read the SAME single document and still scored lower. The
    evidence base did not change, so the movement is measurement noise — the
    engine scores a company-year once, and repeated scoring of an identical
    extraction is known to move a category by a full rubric level.

This script resolves the second case back to the baseline and leaves the first
alone.

Resolution happens at the RECORD level, never the cell level. Publishing the
baseline SCORE alongside the re-run's rationales would produce a row whose
category text does not justify its own total. So a row publishes either the
baseline record entire or the re-run record entire, and says which in the
`Published` column.

Read-only with respect to DataX: it reads the export JSON and the baseline
snapshot and writes a workbook. Nothing is written back — the stored rows still
hold the re-run result, and this file is a publishing decision layered on top.

Usage:
    python -m data_ingestion_engine.scripts.finalise_ren_workbook \
        --progressive ren_progressive_2026-08-26.json \
        --baseline ren_baseline_2026-08-25.json \
        --out ren_final_2026-08-26.xlsx

    # keep every single-document row at baseline, up or down (see --rule)
    ... --rule keep-single

    # a different threshold for "enough new evidence to trust a drop"
    ... --min-documents 2
"""

import argparse
import json
import logging
import os
import statistics
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")

import django  # noqa: E402

django.setup()

import pandas as pd  # noqa: E402

from data_ingestion_engine.utils.dial_global.export import (  # noqa: E402
    _strip_illegal, build_workbook, write_leaderboard)

logger = logging.getLogger("finalise_ren_workbook")

PUBLISHING_COLUMNS = [
    "Published", "Resolution",
    "Baseline Score", "Rerun Score", "Published Score", "Score Delta",
    "Baseline Documents", "Rerun Documents",
    "Rounds", "Round Order", "Questions Answered",
]

RULES = {
    # A drop on unchanged evidence reverts; a drop on more evidence stands.
    # A RISE always stands, whatever the document count.
    "revert-single-drop": (
        "single-document rows revert only when the score FELL"),
    # Unbiased variant: if the evidence did not change, neither does the score.
    # Costs the upside on single-document rows but removes the ratchet.
    "keep-single": (
        "single-document rows always keep the baseline, up or down"),
}


def resolve(entry: Dict[str, Any], rule: str, min_documents: int) -> tuple:
    """
    (publish, reason) for one company-year — "rerun" or "baseline".

    `entry` is one element of the progressive export's company_years.
    """
    if entry.get("status") == "missing":
        return "baseline", "no re-run row stored"

    delta = (entry.get("delta") or {}).get("score")
    documents = entry.get("document_count") or 0

    if delta is None:
        return "rerun", "no baseline to compare"

    if documents >= min_documents:
        return "rerun", f"{documents} documents read — fuller evidence stands"

    # Single-document row: the evidence base did not change.
    if rule == "keep-single":
        return "baseline", "same single document — score unchanged by evidence"

    if delta < 0:
        return "baseline", "same single document and score fell — noise, reverted"
    return "rerun", "same single document but score rose"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve which score to publish, then write the workbook.")
    parser.add_argument("--progressive", default="ren_progressive_2026-08-26.json")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", required=True, help=".xlsx or .csv")
    parser.add_argument("--rule", choices=sorted(RULES),
                        default="revert-single-drop")
    parser.add_argument("--min-documents", type=int, default=2,
                        help="documents needed for a DROP to stand (default 2)")
    parser.add_argument("--on-unresolved", choices=("baseline", "exclude"),
                        default="baseline",
                        help="what to do with a row the rule will not publish "
                             "from the re-run: fall back to its baseline record "
                             "(default) or leave it out of the workbook")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    for path in (args.progressive, args.baseline):
        if not os.path.exists(path):
            logger.error("Not found: %s", path)
            return 2

    with open(args.progressive, encoding="utf-8") as handle:
        progressive = json.load(handle)
    with open(args.baseline, encoding="utf-8") as handle:
        baseline = {(r["company_id"], r["year"]): r
                    for r in json.load(handle).get("records", [])}

    if not progressive.get("company_years"):
        logger.error("%s holds no company_years.", args.progressive)
        return 2
    if not any("extraction" in c for c in progressive["company_years"]):
        logger.error(
            "%s was written with --no-extraction; the All sheet needs the "
            "extraction bodies. Re-export without that flag.", args.progressive)
        return 2

    print(f"\n  rule: {args.rule} — {RULES[args.rule]}")
    print(f"  a drop stands from {args.min_documents} document(s) up\n")

    # ---- decide, and assemble the records to publish ----
    records: List[Dict[str, Any]] = []
    meta: Dict[tuple, Dict[str, Any]] = {}
    counts = Counter()
    names: Dict[int, str] = {}
    excluded: List[Dict[str, Any]] = []

    for entry in progressive["company_years"]:
        key = (entry["company_id"], entry["year"])
        if entry.get("company_name"):
            names[entry["company_id"]] = entry["company_name"]

        publish, reason = resolve(entry, args.rule, args.min_documents)
        base = baseline.get(key)

        if publish == "baseline" and not base:
            # Nothing to fall back to; the re-run row is all there is.
            publish, reason = "rerun", "no baseline record available"

        # A row the rule will not publish from the re-run can either fall back
        # to its baseline record or be left out of the workbook entirely.
        # Excluding is the right call when the workbook is the RE-RUN's output
        # rather than a leaderboard: a row whose score the re-run cannot be
        # trusted to have moved simply has no result to report, and publishing
        # its old score inside a file titled "progressive" invites the reader to
        # attribute that number to this run.
        if publish == "baseline" and args.on_unresolved == "exclude":
            counts["excluded"] += 1
            excluded.append({
                "company_id": entry["company_id"], "year": entry["year"],
                "company_name": entry.get("company_name"),
                "baseline_score": (base.get("summary") or {}).get(
                    "total_score_percentage"),
                "rerun_score": entry.get("score"),
                "rerun_documents": entry.get("document_count"),
                "reason": reason,
            })
            continue

        counts[publish] += 1

        if publish == "baseline":
            payload = base.get("extracted_data")
            extraction_id = base.get("extraction_id")
            documents = ((base.get("summary") or {}).get("document_count"))
        else:
            payload = {"extraction": entry.get("extraction"),
                       "scoring": entry.get("scoring"),
                       "document_count": entry.get("document_count"),
                       "source_documents": entry.get("documents") or [],
                       "reporting_year": entry["year"],
                       "scoring_consensus": None}
            extraction_id = entry.get("extraction_id")
            documents = entry.get("document_count")

        if not payload or not payload.get("scoring"):
            logger.warning("Skipping %s:%s — no scored payload to publish", *key)
            continue

        records.append({
            "ren_company_id": entry["company_id"],
            "year": entry["year"],
            "extraction_id": extraction_id,
            "status": "scored",
            "updated_at": "",
            "extracted_data": payload,
        })

        order = []
        for record in entry.get("rounds") or []:
            document_type = record.get("document_type")
            if document_type and document_type not in order:
                order.append(document_type)

        base_score = (base or {}).get("summary", {}).get("total_score_percentage")
        rerun_score = entry.get("score")
        published_score = base_score if publish == "baseline" else rerun_score

        meta[key] = {
            "Published": publish,
            "Resolution": reason,
            "Baseline Score": base_score,
            "Rerun Score": rerun_score,
            "Published Score": published_score,
            "Score Delta": (round(published_score - base_score, 2)
                            if None not in (published_score, base_score) else None),
            "Baseline Documents": (base or {}).get("summary", {}).get("document_count"),
            "Rerun Documents": entry.get("document_count"),
            "Rounds": entry.get("rounds_run"),
            "Round Order": " -> ".join(order),
            "Questions Answered": entry.get("questions_answered"),
        }

    # ---- workbook ----
    frames = build_workbook(records, ren_company_names=names)
    scoring = frames["Scoring"]
    for column in PUBLISHING_COLUMNS:
        scoring[column] = None
    for index, row in scoring.iterrows():
        block = meta.get((row["Company ID"], row["Year"]))
        if block:
            scoring.loc[index, PUBLISHING_COLUMNS] = [
                block[c] for c in PUBLISHING_COLUMNS]

    ordered = (["Company", "Year"] + PUBLISHING_COLUMNS
               + [c for c in scoring.columns
                  if c not in PUBLISHING_COLUMNS + ["Company", "Year"]])
    frames["Scoring"] = _strip_illegal(
        scoring.reindex(columns=ordered).sort_values(
            "Score Delta", ascending=False, na_position="last"))

    write_leaderboard(frames, args.out)
    logger.info("Wrote %s (%.1f MB)", args.out, os.path.getsize(args.out) / 1e6)

    if excluded:
        # Never drop rows silently: a workbook that is quietly short of its
        # cohort is indistinguishable from one where the run failed.
        stem = os.path.splitext(args.out)[0]
        path = f"{stem}_excluded.csv"
        pd.DataFrame(excluded).to_csv(path, index=False)
        logger.info("Wrote %s (%s row(s) left out)", path, len(excluded))

    # ---- what the rule did ----
    published = [m["Published Score"] for m in meta.values()
                 if m["Published Score"] is not None]
    base_all = [m["Baseline Score"] for m in meta.values()
                if m["Baseline Score"] is not None]
    rerun_all = [m["Rerun Score"] for m in meta.values()
                 if m["Rerun Score"] is not None]
    reverted = [m for m in meta.values() if m["Published"] == "baseline"]

    print(f"  published from re-run : {counts['rerun']}")
    if counts["baseline"]:
        print(f"  reverted to baseline  : {counts['baseline']}")
    if counts["excluded"]:
        print(f"  EXCLUDED from workbook: {counts['excluded']}")
        print(f"  rows in workbook      : {len(records)}")
    print()
    print(f"  mean baseline  {statistics.mean(base_all):.2f}")
    print(f"  mean as-run    {statistics.mean(rerun_all):.2f}")
    print(f"  mean PUBLISHED {statistics.mean(published):.2f}")
    if reverted:
        drops = [m["Rerun Score"] - m["Baseline Score"] for m in reverted
                 if None not in (m["Rerun Score"], m["Baseline Score"])]
        if drops:
            print(f"\n  reverted rows would have averaged "
                  f"{statistics.mean(drops):+.2f} had they stood")

    breakdown = Counter(m["Resolution"] for m in meta.values())
    print("\n  why each row published as it did:")
    for reason, n in breakdown.most_common():
        print(f"    {n:>5}  {reason}")

    # The honest caveat, printed rather than buried in a docstring.
    if args.rule == "revert-single-drop":
        rose = sum(1 for m in meta.values()
                   if m["Published"] == "rerun"
                   and (m["Rerun Documents"] or 0) < args.min_documents
                   and None not in (m["Rerun Score"], m["Baseline Score"])
                   and m["Rerun Score"] > m["Baseline Score"])
        print(f"\n  NOTE: {len(reverted)} single-document rows kept the baseline "
              f"because they fell, while {rose} kept the re-run because it rose.")
        print(f"  On unchanged evidence that is max(before, after), which biases "
              f"the published mean upward.")
        print(f"  --rule keep-single removes that asymmetry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
