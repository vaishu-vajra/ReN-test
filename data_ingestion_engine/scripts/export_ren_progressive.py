#!/usr/bin/env python
"""
The progressive run's deliverable: one JSON holding every company-year's result,
its round-by-round trace, and its delta against the pre-run baseline.

Read-only. Fetches the stored rows, joins them to the baseline snapshot taken
before the run, and writes one file. Writes nothing to DataX.

Reads year by year and unions on extraction_id — `/internal/dial-global/
extractions/` 500s unfiltered on prod, so this is not an optimisation.

Usage:
    python -m data_ingestion_engine.scripts.export_ren_progressive \
        --uid <uid> \
        --api-url https://<prod-datax-host> \
        --baseline ren_baseline_2026-08-25.json \
        --out ren_progressive_2026-08-25.json

    # smaller file for sharing: drop the 64-question extraction bodies
    ... --no-extraction

The leaderboard endpoint times out at this row count (gunicorn 120s), so the
reliable path is to run this inside a celery pod and `kubectl cp` the file out —
pods have 10 GiB and no HTTP timeout.
"""

import argparse
import datetime
import json
import logging
import os
import statistics
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.dial_global.export import (  # noqa: E402
    _strip_illegal, build_workbook, write_leaderboard)
from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: E402
    DIAL_GLOBAL_BASE, fetch_dial_global_extractions)
from data_ingestion_engine.utils.ren.company_list import (  # noqa: E402
    load_flagged_pairs)

# Prepended to the Scoring sheet. The cohort exists because these rows scored
# badly on one document, so what the re-run changed is the point of the file and
# should not sit 40 columns to the right of the rubric block.
COMPARISON_COLUMNS = [
    "Baseline Score", "New Score", "Score Delta",
    "Baseline Documents", "New Documents",
    "Rounds", "Rounds Failed", "Round Order",
    "Questions Answered", "Baseline Questions", "Questions Delta",
]

logger = logging.getLogger("export_ren_progressive")

# The cohort currently being worked. Every script here takes --csv, so an
# earlier cohort stays runnable by naming it explicitly.
DEFAULT_CSV = ("data_ingestion_engine/"
               "Below 70 - below 70.csv")
# Which company column a row hangs off. ReN results index the canonical
# `companies` table; DIAL Global results index dial_global_companies. The two id
# spaces overlap numerically, so reading the wrong column does not fail — it
# silently returns another universe's companies.
REN_COMPANY_FIELD = "ren_company_id"
DG_COMPANY_FIELD = "company_id"

# Below this, a stored `year` is bad data rather than a reporting year.
MIN_SANE_YEAR = 2000
CATEGORIES = ("culture", "inclusion", "talent", "impact", "equity", "social")


def score_of(payload: Dict[str, Any]) -> Optional[float]:
    scoring = (payload or {}).get("scoring") or {}
    return (scoring.get("overall_score") or {}).get("total_score_percentage")


def category_scores(payload: Dict[str, Any]) -> Dict[str, Any]:
    categories = ((payload or {}).get("scoring") or {}).get("categories") or {}
    return {name: (categories.get(name) or {}).get("score") for name in CATEGORIES}


def answered_count(payload: Dict[str, Any]) -> Optional[int]:
    """
    How many of the 64 questions carry a score, or None if it cannot be known.

    Progressive rows record this directly; joint rows predate the field, so it
    is counted off the extraction body to keep the two comparable.

    Returns None — never 0 — when there is no extraction body to count. A
    baseline rebuilt from a scored export (see baseline_from_csv.py) has scores
    but no questions, and reporting that as "0 answered" would show every row
    gaining its entire question count out of nowhere.
    """
    stored = (payload or {}).get("questions_answered")
    if stored is not None:
        return int(stored)
    extraction = (payload or {}).get("extraction")
    if not extraction:
        return None
    return sum(
        1 for section in extraction.values() if isinstance(section, dict)
        for value in section.values()
        if isinstance(value, dict) and value.get("score") == 1)


def _delta(after, before):
    if after is None or before is None:
        return None
    return round(after - before, 4)



# Companies queried at once when falling back from a failed year read. Each
# query is (company, year) filtered, so the response is one row — the cost is
# round trips, not payload.
YEAR_FALLBACK_WORKERS = int(os.getenv("EXPORT_YEAR_FALLBACK_WORKERS", "12"))



def _years_of(record: Dict[str, Any]) -> List[int]:
    """
    The reporting years a resolver recorded, however it recorded them.

    The two resolvers disagree on shape: resolve_company_cohort writes a list of
    ints, dg_company_ids writes a {year: document_count} dict — and a JSON round
    trip turns that dict's keys into strings. Both are legitimate files to point
    --resolved at, so normalise here rather than making the caller know which
    tool produced the file.
    """
    raw = record.get("years") or []
    if isinstance(raw, dict):
        raw = list(raw.keys())
    years = []
    for value in raw:
        try:
            years.append(int(value))
        except (TypeError, ValueError):
            continue
    return years


def _rows_for_year_per_company(uid: str, year: int, company_ids: List[int],
                               company_field: str) -> List[Dict[str, Any]]:
    """
    One year's rows, read a company at a time.

    The bulk year read is one query returning thousands of JSONB payloads and it
    fails under load. Pushing the company id down as well makes each query
    trivial, at the cost of one round trip per company. Slower, but it answers.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(company_id: int) -> List[Dict[str, Any]]:
        try:
            return fetch_dial_global_extractions(
                uid, company_id=company_id, year=year,
                company_field=company_field) or []
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
        parents=[_PRE], description="Export the progressive ReN run as JSON.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--universe", choices=("ren", "dg"), default="ren",
                        help="which company column results hang off. 'dg' for "
                             "DIAL Global (dial_global_companies), 'ren' for "
                             "the canonical companies table.")
    parser.add_argument("--csv", default=DEFAULT_CSV,
                        help="scored cohort export, to know which pairs to fetch")
    parser.add_argument("--resolved",
                        help="dgi_resolved.json from resolve_company_cohort.py. "
                             "Use this for a cohort that was never scored "
                             "before: there are no (company_id, year) pairs to "
                             "name and no baseline to compare against, so every "
                             "year the run produced for those companies is "
                             "exported and the delta columns stay null.")
    parser.add_argument("--max-score", type=float, default=70.0)
    parser.add_argument("--baseline",
                        help="ren_baseline_*.json from snapshot_ren_baseline.py")
    parser.add_argument("--out", required=True, help="JSON output path")
    parser.add_argument("--workbook",
                        help="also write the Scoring/All workbook here "
                             "(.xlsx for both sheets, .csv for Scoring only)")
    parser.add_argument("--no-extraction", action="store_true",
                        help="omit the 64-question extraction bodies")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    company_field = (DG_COMPANY_FIELD if args.universe == "dg"
                     else REN_COMPANY_FIELD)
    logger.info("universe=%s, keying on %s", args.universe, company_field)

    if args.resolved:
        if not os.path.exists(args.resolved):
            logger.error("Resolved cohort not found: %s", args.resolved)
            return 2
        with open(args.resolved, encoding="utf-8") as handle:
            resolved = json.load(handle)
        ready = [r for r in resolved.get("results", [])
                 if r.get("status") == "ready" and r.get("company_id")]
        company_ids = {int(r["company_id"]) for r in ready}
        names = {int(r["company_id"]): r.get("company")
                 or r.get("matched_name") for r in ready}

        # No pair list to match on: the run chose each company's own years, so
        # take every year it could plausibly have produced. The years recorded
        # during RESOLUTION are not sufficient on their own — resolution ran
        # before the reporting-year sanity bound existed, so for a handful of
        # companies it recorded 2093 or 2085 where the run actually did 2026 and
        # 2025. The recent-year window covers what those fell back to.
        this_year = datetime.date.today().year
        years_to_read = {y for r in ready for y in _years_of(r)
                         if MIN_SANE_YEAR <= y <= this_year + 1}
        years_to_read |= set(range(this_year - 4, this_year + 2))
        wanted = None          # every (company_id, year) this run produced
        logger.info("%s company/companies from %s; reading years %s",
                    len(company_ids), args.resolved,
                    sorted(years_to_read, reverse=True))
    else:
        if not os.path.exists(args.csv):
            logger.error("Cohort file not found: %s", args.csv)
            return 2
        pairs, _ = load_flagged_pairs(args.csv, max_score=args.max_score)
        wanted = {(p["company_id"], p["year"]) for p in pairs}
        company_ids = {p["company_id"] for p in pairs}
        names = {p["company_id"]: p["company_name"] for p in pairs}
        years_to_read = {p["year"] for p in pairs}
        logger.info("%s cohort pair(s)", len(wanted))

    baseline: Dict[tuple, Dict[str, Any]] = {}
    if args.baseline:
        if not os.path.exists(args.baseline):
            logger.error("Baseline not found: %s", args.baseline)
            return 2
        with open(args.baseline, encoding="utf-8") as handle:
            for record in json.load(handle).get("records", []):
                baseline[(record["company_id"], record["year"])] = record
        logger.info("%s baseline row(s) loaded", len(baseline))
    else:
        logger.warning("No --baseline given; deltas will be null.")

    # ---- fetch, year by year ----
    by_key: Dict[tuple, Dict[str, Any]] = {}
    seen = set()
    for year in sorted(years_to_read, reverse=True):
        try:
            rows = fetch_dial_global_extractions(
                args.uid, year=year, company_field=company_field)
        except Exception as e:
            logger.error("Read failed for year=%s: %s", year, e)
            return 1

        if not rows:
            # A year-filtered read that comes back empty is not proof the year
            # is empty. /extractions/ 500s under load — it already does so
            # unfiltered, and the filtered read starts failing too as the table
            # grows. Silently accepting the empty answer drops a whole year from
            # the export, which for 2025 is most of the cohort. Confirm it
            # company by company, where each query is small enough to survive.
            logger.warning(
                "year=%s came back empty — re-reading per company to tell an "
                "empty year from a failed request", year)
            rows = _rows_for_year_per_company(
                args.uid, year, sorted(company_ids), company_field)
            logger.info("year=%s: %s row(s) recovered per-company",
                        year, len(rows))

        kept = 0
        for row in rows:
            company_id = row.get(company_field)
            if company_id is None:
                continue
            key = (int(company_id), int(row.get("year") or 0))
            if row.get("extraction_id") in seen:
                continue
            if wanted is not None:
                if key not in wanted:
                    continue
            elif key[0] not in company_ids:
                continue
            seen.add(row.get("extraction_id"))
            incumbent = by_key.get(key)
            if incumbent is None or (row.get("extraction_id") or 0) > (
                    incumbent.get("extraction_id") or 0):
                by_key[key] = row
            kept += 1
        logger.info("year=%s: %s row(s), %s in cohort", year, len(rows), kept)

    # ---- assemble ----
    company_years: List[Dict[str, Any]] = []
    mode_counts = Counter()
    deltas, before_scores, after_scores = [], [], []
    docs_before, docs_after = [], []
    answered_before, answered_after = [], []
    rounds_total = rounds_failed = 0
    # Which report type ended up answering questions, across the whole run. This
    # is what says whether widening from four types to eleven actually paid.
    fills_by_type = Counter()
    rounds_by_type = Counter()

    # With an explicit pair list, a pair with no stored row is a MISSING result
    # and has to be reported. With a company-id cohort there is no expected year
    # to be missing, so only what was actually produced is exported.
    for key in sorted(wanted if wanted is not None else by_key):
        row = by_key.get(key)
        company_id, year = key
        if row is None:
            company_years.append({
                "company_id": company_id, "year": year,
                "company_name": names.get(company_id),
                "status": "missing",
                "note": "no stored row — the run did not produce one",
            })
            continue

        payload = row.get("extracted_data") or {}
        mode = payload.get("extraction_mode", "joint")
        mode_counts[mode] += 1

        after_score = score_of(payload)
        after_docs = payload.get("document_count")
        after_answered = answered_count(payload)

        base = baseline.get(key)
        base_summary = (base or {}).get("summary") or {}
        base_payload = (base or {}).get("extracted_data") or {}
        before_score = base_summary.get("total_score_percentage")
        before_docs = base_summary.get("document_count")
        before_answered = answered_count(base_payload) if base else None

        rounds = payload.get("extraction_rounds") or []
        rounds_total += len(rounds)
        rounds_failed += sum(1 for r in rounds if r.get("error"))
        for record in rounds:
            document_type = record.get("document_type") or "(unknown)"
            rounds_by_type[document_type] += 1
            fills_by_type[document_type] += record.get("questions_filled") or 0

        if after_score is not None:
            after_scores.append(after_score)
        if before_score is not None:
            before_scores.append(before_score)
        if after_score is not None and before_score is not None:
            deltas.append(after_score - before_score)
        if after_docs is not None:
            docs_after.append(after_docs)
        if before_docs is not None:
            docs_before.append(before_docs)
        answered_after.append(after_answered)
        if before_answered is not None:
            answered_before.append(before_answered)

        entry: Dict[str, Any] = {
            "company_id": company_id,
            "year": year,
            "company_name": names.get(company_id),
            "status": row.get("status"),
            "extraction_mode": mode,
            "extraction_id": row.get("extraction_id"),
            "updated_at": row.get("updated_at"),
            "source_report_ids": row.get("source_report_ids"),
            "documents": payload.get("source_documents") or [],
            "document_count": after_docs,
            "rounds": rounds,
            "round_unit": payload.get("round_unit"),
            "rounds_run": payload.get("rounds_run"),
            "rounds_failed": payload.get("rounds_failed"),
            "report_types_available": payload.get("report_types_available"),
            "documents_available": payload.get("documents_available"),
            # question_id -> {round, document_type, report_ids}. A round reads
            # every document of one type together, so an answer is attributed to
            # the type and the set of files in that call; which specific file a
            # quote came from is named in the justification itself.
            "answer_provenance_by_type": {
                question_id: (source or {}).get("document_type")
                for question_id, source in
                (payload.get("answer_provenance") or {}).items()},
            "questions_answered": after_answered,
            "questions_total": payload.get("questions_total", 64),
            "answer_provenance": payload.get("answer_provenance") or {},
            "organisation_provenance": payload.get("organisation_provenance") or {},
            "score": after_score,
            "category_scores": category_scores(payload),
            "scoring": payload.get("scoring"),
            "baseline": {
                "score": before_score,
                "document_count": before_docs,
                "questions_answered": before_answered,
                "source_documents": base_summary.get("source_documents"),
                "category_scores": base_summary.get("category_scores"),
                "extraction_id": (base or {}).get("extraction_id"),
            } if base else None,
            "delta": {
                "score": _delta(after_score, before_score),
                "document_count": _delta(after_docs, before_docs),
                "questions_answered": _delta(after_answered, before_answered),
            } if base else None,
        }
        if not args.no_extraction:
            entry["extraction"] = payload.get("extraction")
        company_years.append(entry)

    def mean(values):
        return round(statistics.mean(values), 2) if values else None

    result = {
        "run": {
            "cohort_source": args.csv,
            "max_score": args.max_score,
            "baseline": args.baseline,
            "datax_base": DIAL_GLOBAL_BASE,
            "pairs_requested": len(wanted) if wanted is not None else len(by_key),
            "pairs_found": len(by_key),
            "pairs_missing": (len(wanted) - len(by_key)) if wanted is not None else 0,
            "extraction_modes": dict(mode_counts),
            "includes_extraction_bodies": not args.no_extraction,
        },
        "totals": {
            "mean_score_before": mean(before_scores),
            "mean_score_after": mean(after_scores),
            "mean_score_delta": mean(deltas),
            "improved": sum(1 for d in deltas if d > 0),
            "unchanged": sum(1 for d in deltas if d == 0),
            "regressed": sum(1 for d in deltas if d < 0),
            "mean_documents_before": mean(docs_before),
            "mean_documents_after": mean(docs_after),
            "mean_questions_answered_before": mean(answered_before),
            "mean_questions_answered_after": mean(answered_after),
            "extraction_rounds_total": rounds_total,
            "extraction_rounds_failed": rounds_failed,
            "document_count_distribution": dict(sorted(Counter(docs_after).items())),
            # Per report type: how many rounds it ran, how many questions it
            # answered, and the yield per round. A type with a high round count
            # and near-zero yield is one to drop from REN_ROUND_ORDER.
            "yield_by_report_type": {
                document_type: {
                    "rounds": rounds_by_type[document_type],
                    "questions_filled": fills_by_type[document_type],
                    "questions_per_round": round(
                        fills_by_type[document_type] / rounds_by_type[document_type], 2)
                    if rounds_by_type[document_type] else 0,
                }
                for document_type in sorted(
                    rounds_by_type, key=lambda t: -fills_by_type[t])},
        },
        "company_years": company_years,
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)

    size_mb = os.path.getsize(args.out) / 1e6
    logger.info("Wrote %s (%s company-year(s), %.1f MB)",
                args.out, len(company_years), size_mb)

    # ---- the workbook ----
    #
    # Same two sheets as the full leaderboard — build_workbook() owns the column
    # definitions, so this file and dial_global_final.xlsx cannot drift — but
    # restricted to the cohort, and with the before/after block on the front of
    # the Scoring sheet.
    if args.workbook:
        frames = build_workbook(by_key.values(), ren_company_names=names)

        comparison = {}
        for entry in company_years:
            if entry.get("status") == "missing":
                continue
            comparison[(entry["company_id"], entry["year"])] = entry

        scoring = frames["Scoring"]
        for column in COMPARISON_COLUMNS:
            scoring[column] = None

        for index, row in scoring.iterrows():
            entry = comparison.get((row["Company ID"], row["Year"]))
            if not entry:
                continue
            base = entry.get("baseline") or {}
            delta = entry.get("delta") or {}
            order = []
            for record in entry.get("rounds") or []:
                document_type = record.get("document_type")
                if document_type and document_type not in order:
                    order.append(document_type)
            scoring.loc[index, COMPARISON_COLUMNS] = [
                base.get("score"), entry.get("score"), delta.get("score"),
                base.get("document_count"), entry.get("document_count"),
                entry.get("rounds_run"), entry.get("rounds_failed"),
                " -> ".join(order),
                entry.get("questions_answered"),
                base.get("questions_answered"), delta.get("questions_answered"),
            ]

        # Comparison first, then the workbook's own columns.
        ordered = (["Company", "Year"] + COMPARISON_COLUMNS
                   + [c for c in scoring.columns
                      if c not in COMPARISON_COLUMNS + ["Company", "Year"]])
        # Re-strip: build_workbook() cleaned the frame before the comparison
        # columns were added, and Company Name and Round Order are assembled
        # from stored values that came out of the same PDFs.
        frames["Scoring"] = _strip_illegal(
            scoring.reindex(columns=ordered).sort_values(
                "Score Delta", ascending=False, na_position="last"))

        write_leaderboard(frames, args.workbook)
        logger.info("Wrote %s (%.1f MB)", args.workbook,
                    os.path.getsize(args.workbook) / 1e6)

    totals = result["totals"]
    print("\n--- progressive run ---")
    # `wanted` is None for a company-id cohort: there is no expected pair list,
    # so "found out of requested" has no denominator to report.
    if wanted is not None:
        print(f"  pairs:      {len(by_key)}/{len(wanted)} found  "
              f"modes={dict(mode_counts)}")
    else:
        print(f"  rows:       {len(by_key)} company-year(s) across "
              f"{len({k[0] for k in by_key})} company/companies  "
              f"modes={dict(mode_counts)}")
    print(f"  score:      {totals['mean_score_before']} -> "
          f"{totals['mean_score_after']}  (mean delta {totals['mean_score_delta']})")
    print(f"  documents:  {totals['mean_documents_before']} -> "
          f"{totals['mean_documents_after']} per company-year")
    print(f"  questions:  {totals['mean_questions_answered_before']} -> "
          f"{totals['mean_questions_answered_after']} of 64 answered")
    print(f"  rounds:     {totals['extraction_rounds_total']} run, "
          f"{totals['extraction_rounds_failed']} failed "
          f"(one round = one report type)")
    print(f"  outcomes:   {totals['improved']} improved, "
          f"{totals['unchanged']} unchanged, {totals['regressed']} regressed")
    if totals["yield_by_report_type"]:
        print("\n--- which report types earned their round ---")
        for document_type, stats in totals["yield_by_report_type"].items():
            print(f"  {stats['questions_filled']:6} questions  "
                  f"{stats['questions_per_round']:6}/round  "
                  f"{stats['rounds']:5} rounds  {document_type}")
    if totals["regressed"]:
        print("\n  A regression is worth reading: the merge only adds answers, so "
              "a lower score means the SCORING pass read the fuller evidence "
              "differently, not that evidence was lost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
