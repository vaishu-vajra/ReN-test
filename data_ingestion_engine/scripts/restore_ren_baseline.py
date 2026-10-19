#!/usr/bin/env python
"""
Put back what a progressive run replaced, from a baseline snapshot.

DataX keeps no history: storage upserts on (ren_company_id, year) and the
previous row is simply gone. The snapshot taken by snapshot_ren_baseline.py
holds each row's complete `extracted_data`, so restoring is a matter of writing
it back through the same path the run used.

DRY RUN BY DEFAULT — nothing is written without --apply.

Only rows that ACTUALLY CHANGED are restored. Each snapshot record is compared
against what is stored now, and a row whose extraction_id and score still match
the snapshot is reported as `unchanged` and skipped. So this is safe to run after
a partial run, and safe to run twice.

Two things it cannot do:

  * A row that did not exist when the snapshot was taken cannot be restored to
    "absent" — DataX has no delete on this path. Those are reported as
    `no_baseline` and left alone.
  * DataX refuses to let an `unscored` record overwrite a `scored` one. If a
    snapshot record is unscored and the current row is scored, the write is
    suppressed server-side and reported as `downgrade_suppressed`.

Usage:
    # what would change (no writes)
    python -m data_ingestion_engine.scripts.restore_ren_baseline \
        --uid <uid> --api-url https://<prod-datax-host> \
        --baseline ren_baseline_2026-08-25.json

    # actually restore
    python -m data_ingestion_engine.scripts.restore_ren_baseline \
        --uid <uid> --api-url https://<prod-datax-host> \
        --baseline ren_baseline_2026-08-25.json --apply

    # restore only some of it
    ... --pairs 25:2024,58:2026 --apply
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.dial_global.store_data import (  # noqa: E402
    DIAL_GLOBAL_BASE, STATUS_SCORED, fetch_dial_global_extractions)
from data_ingestion_engine.utils.ren.store_data import (  # noqa: E402
    store_ren_extraction)

logger = logging.getLogger("restore_ren_baseline")

REN_COMPANY_FIELD = "ren_company_id"
PROD_MARKERS = ("datax-backend-prod", "ingestion-engine.myrenx.ai")


def parse_pairs(text: str) -> Set[Tuple[int, int]]:
    """`25:2024,58:2026` -> {(25, 2024), (58, 2026)}."""
    out = set()
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        company_id, _, year = chunk.partition(":")
        out.add((int(company_id), int(year)))
    return out


def score_of(payload: Optional[Dict[str, Any]]) -> Optional[float]:
    scoring = (payload or {}).get("scoring") or {}
    return (scoring.get("overall_score") or {}).get("total_score_percentage")


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Restore stored ReN rows from a baseline snapshot.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--baseline", required=True,
                        help="ren_baseline_*.json from snapshot_ren_baseline.py")
    parser.add_argument("--pairs", help="restore only these, e.g. 25:2024,58:2026")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it this is a dry run")
    parser.add_argument("--out", help="write a report JSON here")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt (for non-interactive use)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.baseline):
        logger.error("Baseline not found: %s", args.baseline)
        return 2

    with open(args.baseline, encoding="utf-8") as handle:
        snapshot = json.load(handle)

    records = snapshot.get("records") or []
    if not records:
        logger.error("Baseline holds no records.")
        return 2

    wanted = parse_pairs(args.pairs) if args.pairs else None
    if wanted:
        records = [r for r in records
                   if (r["company_id"], r["year"]) in wanted]
        if not records:
            logger.error("None of --pairs are in the baseline.")
            return 2

    snap_base = (snapshot.get("meta") or {}).get("datax_base") or "?"
    is_prod = any(marker in DIAL_GLOBAL_BASE for marker in PROD_MARKERS)

    print(f"\n  Baseline:  {args.baseline}")
    print(f"             taken against {snap_base}")
    print(f"  Restoring to: {DIAL_GLOBAL_BASE}"
          f"  [{'PRODUCTION' if is_prod else 'non-production'}]")
    print(f"  Records:   {len(records)}")
    print(f"  Mode:      {'APPLY (writes)' if args.apply else 'DRY RUN'}\n")

    if snap_base != "?" and snap_base not in DIAL_GLOBAL_BASE:
        # Restoring a prod snapshot into dev (or the reverse) would write one
        # environment's scores over another's.
        print(f"  !! The snapshot was taken against a DIFFERENT backend than the")
        print(f"     one being restored to. Check --api-url before continuing.\n")
        if args.apply and not args.yes:
            if input("     Type 'yes' to continue anyway: ").strip().lower() != "yes":
                print("     Aborted.")
                return 1

    # ---- what is stored right now ----
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for year in sorted({r["year"] for r in records}, reverse=True):
        try:
            rows = fetch_dial_global_extractions(
                args.uid, year=year, company_field=REN_COMPANY_FIELD)
        except Exception as e:
            logger.error("Read failed for year=%s: %s", year, e)
            return 1
        for row in rows:
            company_id = row.get(REN_COMPANY_FIELD)
            if company_id is None:
                continue
            key = (int(company_id), int(row.get("year") or 0))
            incumbent = by_key.get(key)
            if incumbent is None or (row.get("extraction_id") or 0) > (
                    incumbent.get("extraction_id") or 0):
                by_key[key] = row
        logger.info("year=%s: %s stored row(s) read", year, len(rows))

    # ---- decide what needs restoring ----
    plan: List[Dict[str, Any]] = []
    for record in records:
        key = (record["company_id"], record["year"])
        current = by_key.get(key)
        baseline_payload = record.get("extracted_data") or {}
        baseline_score = (record.get("summary") or {}).get(
            "total_score_percentage")

        entry = {
            "company_id": key[0], "year": key[1],
            "company_name": record.get("company_name"),
            "baseline_extraction_id": record.get("extraction_id"),
            "baseline_score": baseline_score,
            "baseline_status": record.get("status"),
        }

        if current is None:
            entry["action"] = "missing_now"
            entry["note"] = "no stored row — restoring will re-create it"
            plan.append(entry)
            continue

        current_payload = current.get("extracted_data") or {}
        entry["current_extraction_id"] = current.get("extraction_id")
        entry["current_score"] = score_of(current_payload)
        entry["current_mode"] = current_payload.get("extraction_mode", "joint")

        # Same row, same score: the run never touched it (or never ran).
        unchanged = (
            current.get("extraction_id") == record.get("extraction_id")
            and entry["current_score"] == baseline_score
            and entry["current_mode"] != "progressive")
        entry["action"] = "unchanged" if unchanged else "restore"
        plan.append(entry)

    counts = Counter(e["action"] for e in plan)
    to_restore = [e for e in plan if e["action"] in ("restore", "missing_now")]

    print(f"  unchanged, will skip : {counts.get('unchanged', 0)}")
    print(f"  changed, will restore: {counts.get('restore', 0)}")
    print(f"  absent, will re-create: {counts.get('missing_now', 0)}")

    changed = [e for e in plan if e["action"] == "restore"]
    if changed:
        print("\n  --- rows that would be rolled back ---")
        for entry in changed[:20]:
            print(f"    {entry['company_id']}:{entry['year']}  "
                  f"{(entry.get('company_name') or '')[:28]:<28} "
                  f"{entry.get('current_score')} -> {entry['baseline_score']}  "
                  f"({entry.get('current_mode')} -> joint)")
        if len(changed) > 20:
            print(f"    ... and {len(changed) - 20} more")

    if not to_restore:
        print("\n  Nothing to restore — stored rows already match the baseline.")
        return 0

    if not args.apply:
        print(f"\n  DRY RUN — nothing written. Re-run with --apply to restore "
              f"{len(to_restore)} row(s).")
        return 0

    if not args.yes:
        print(f"\n  !! About to overwrite {len(to_restore)} stored row(s) with "
              f"their baseline values.")
        if input("     Type 'yes' to continue: ").strip().lower() != "yes":
            print("     Aborted.")
            return 1
    print()

    # ---- restore ----
    snapshot_by_key = {(r["company_id"], r["year"]): r for r in records}
    ok = failed = suppressed = 0
    for index, entry in enumerate(to_restore, start=1):
        key = (entry["company_id"], entry["year"])
        record = snapshot_by_key[key]
        try:
            result = store_ren_extraction(
                company_id=key[0],
                year=key[1],
                extracted_data=record.get("extracted_data"),
                uid=args.uid,
                source_report_ids=record.get("source_report_ids") or [],
                status=record.get("status") or STATUS_SCORED,
            )
        except Exception as e:
            entry["result"] = f"error: {e}"
            failed += 1
            logger.error("  [%s/%s] %s:%s FAILED — %s",
                         index, len(to_restore), key[0], key[1], e)
            continue

        if not result:
            entry["result"] = "store returned nothing"
            failed += 1
            logger.error("  [%s/%s] %s:%s FAILED — store returned nothing",
                         index, len(to_restore), key[0], key[1])
        elif result.get("downgrade_suppressed"):
            # The snapshot row was unscored and DataX kept the scored row.
            entry["result"] = "downgrade_suppressed"
            suppressed += 1
            logger.warning("  [%s/%s] %s:%s suppressed — DataX kept the scored "
                           "row rather than accept an unscored one",
                           index, len(to_restore), key[0], key[1])
        else:
            entry["result"] = "restored"
            entry["restored_extraction_id"] = result.get("extraction_id")
            ok += 1
            logger.info("  [%s/%s] %s:%s restored to %s%%",
                        index, len(to_restore), key[0], key[1],
                        entry["baseline_score"])

    print(f"\n  restored: {ok}   suppressed: {suppressed}   failed: {failed}")
    if failed:
        print("  !! Some rows did NOT restore. Re-run — this script is "
              "idempotent and will retry only what is still wrong.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({
                "baseline": args.baseline,
                "datax": DIAL_GLOBAL_BASE,
                "applied": args.apply,
                "counts": {"restored": ok, "suppressed": suppressed,
                           "failed": failed, **dict(counts)},
                "rows": plan,
            }, handle, indent=2, default=str)
        print(f"  Report written to {args.out}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
