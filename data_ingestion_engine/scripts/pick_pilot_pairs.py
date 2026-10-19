#!/usr/bin/env python
"""
Choose a pilot cohort from an audit, and print the commands to run it.

Picking the ten company-years with the most documents would prove nothing: they
are the easy case, and they are not what the other 787 look like. This selects a
deliberate SPREAD, so a pilot answers the questions actually worth asking before
committing 797 runs:

  multi_round     several report types -> does carrying gaps forward across
                  types actually fill anything? The core claim.
  multi_doc_type  several documents of ONE type in one call -> does reading a
                  category together work, and does it blow the context window?
  single_round    one type, as before -> the control. The score should move
                  very little; if it jumps, the change is not doing what we
                  think it is.
  empty           no recognised documents -> must no-op safely and leave the
                  existing stored row untouched.

Reads ren_type_audit.json from scripts/audit_ren_document_types.py.

Usage:
    python -m data_ingestion_engine.scripts.pick_pilot_pairs
    python -m data_ingestion_engine.scripts.pick_pilot_pairs --count 10
    python -m data_ingestion_engine.scripts.pick_pilot_pairs --json-only
"""

import argparse
import collections
import json
import os
import sys


def band(entry):
    """Which pilot band a company-year belongs to."""
    types = entry.get("new_types") or []
    rounds = entry.get("new_rule_rounds") or 0
    if not types:
        return "empty"
    if rounds >= 2:
        return "multi_round"
    # One round, but more than one document in it.
    if len(types) > 1:
        return "multi_doc_type"
    return "single_round"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pick a spread of pilot company-years from an audit.")
    parser.add_argument("--audit", default="ren_type_audit.json")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--json-only", action="store_true",
                        help="print just the pairs array")
    args = parser.parse_args()

    if not os.path.exists(args.audit):
        print(f"Audit not found: {args.audit}\n"
              f"Run: python -m data_ingestion_engine.scripts."
              f"audit_ren_document_types --uid <uid>", file=sys.stderr)
        return 2

    with open(args.audit, encoding="utf-8") as handle:
        audit = json.load(handle)

    bands = collections.defaultdict(list)
    for entry in audit.get("company_years", []):
        bands[band(entry)].append(entry)

    # Most rounds first inside each band, so the multi_round picks are the ones
    # that exercise the most document types.
    for entries in bands.values():
        entries.sort(key=lambda e: (-(e.get("new_rule_rounds") or 0),
                                    -(e.get("new_rule_documents") or 0)))

    # Weighted so most of the pilot is the case the change is meant to fix,
    # while still carrying controls.
    quota = {"multi_round": 0.4, "multi_doc_type": 0.3,
             "single_round": 0.2, "empty": 0.1}
    picked, seen = [], set()
    for name, share in quota.items():
        want = max(1, round(args.count * share))
        for entry in bands.get(name, [])[:want]:
            key = (entry["company_id"], entry["year"])
            if key in seen:
                continue
            seen.add(key)
            picked.append({**entry, "band": name})

    # Backfill from the richest bands if a band was short.
    if len(picked) < args.count:
        for name in ("multi_round", "multi_doc_type", "single_round"):
            for entry in bands.get(name, []):
                if len(picked) >= args.count:
                    break
                key = (entry["company_id"], entry["year"])
                if key in seen:
                    continue
                seen.add(key)
                picked.append({**entry, "band": name})

    picked = picked[:args.count]
    pairs = [[p["company_id"], p["year"]] for p in picked]

    if args.json_only:
        print(json.dumps(pairs))
        return 0

    print(f"\n=== pilot cohort: {len(picked)} company-year(s) ===\n")
    width = max((len(p.get("company_name") or "") for p in picked), default=20)
    for entry in picked:
        name = (entry.get("company_name") or "?")[:width]
        print(f"  {entry['company_id']:>7}:{entry['year']}  {name:<{width}}  "
              f"{entry['new_rule_rounds']} round(s), "
              f"{entry['new_rule_documents']} doc(s)  [{entry['band']}]")
        if entry.get("round_order"):
            print(f"          {' -> '.join(entry['round_order'])}")

    counts = collections.Counter(p["band"] for p in picked)
    print(f"\n  bands: {dict(counts)}")
    rounds = sum(p["new_rule_rounds"] for p in picked)
    print(f"  ~{rounds} extraction + {sum(1 for p in picked if p['new_types'])} "
          f"scoring = ~{rounds + sum(1 for p in picked if p['new_types'])} "
          f"model calls")

    pair_arg = ",".join(f"{c}:{y}" for c, y in pairs)
    print("\n--- 1. LOCAL, writes nothing (do this first) ---\n")
    print(f"./venv/bin/python -m data_ingestion_engine.scripts."
          f"run_progressive_local \\\n"
          f"    --uid $UID --pairs {pair_arg} \\\n"
          f"    --out pilot.json")

    print("\n--- 2. via the endpoint — THIS WRITES, snapshot first ---\n")
    print("./venv/bin/python -m data_ingestion_engine.scripts."
          "snapshot_ren_baseline \\\n"
          "    --uid $UID --out ren_baseline_$(date +%F).json\n")
    print("curl -X POST http://localhost:8000/ren/extract/progressive/ \\\n"
          '  -H "Authorization: Bearer $TOKEN" '
          '-H "Content-Type: application/json" \\\n'
          f"  -d '{json.dumps({'pairs': pairs, 'batch_size': 5})}'")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
