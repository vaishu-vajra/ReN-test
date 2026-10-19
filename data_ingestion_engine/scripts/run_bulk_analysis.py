#!/usr/bin/env python
"""
Run risk analysis, gap analysis and earnings delta over a cohort. Costs money.

Consumes the JSON from build_analysis_cohort.py and makes the model calls it
lists, in-process — no Celery, no web server. The API views are not called over
HTTP: they stream and sit behind Firebase auth. The same underlying functions
are used instead, so the output and the storage rules are identical to what the
product does.

WRITES ARE OFF BY DEFAULT. Without --store nothing is sent to DataX and results
land in a local JSON.

EXISTING ANALYSIS IS NEVER REPLACED. A section that already holds a value is
skipped twice over: before the model call, so no money is spent on it, and again
against a fresh read immediately before the write, so a value that appeared
while the call was running is not clobbered either. Only --overwrite lifts that,
and it is never on by default. This matters because patch_report is a blind
PATCH and this repo's .env points at the PROD backend.

Three levels, cheapest first:

  --plan      Print the calls that WOULD run, grouped. Zero model calls, zero
              cost. Start here.
  (default)   Run the model for real, write results to a local JSON. Costs model
              calls. Changes nothing in DataX.
  --store     Additionally persist each result into sections that are EMPTY.

Resumable: every finished unit is appended to a state file (--state, default
alongside --out). Re-running skips what already succeeded, so a crash or a
Ctrl-C halfway through 10,000 calls does not re-burn the spend.

Usage:
    # what would run, for free
    python -m data_ingestion_engine.scripts.run_bulk_analysis \
        --uid <uid> --cohort sp500_cohort.json --plan

    # 20 real calls against dev, results kept local
    python -m data_ingestion_engine.scripts.run_bulk_analysis \
        --uid <uid> --cohort sp500_cohort.json \
        --api-url https://datax-backend-dev-...run.app --limit 20

    # the real thing
    python -m data_ingestion_engine.scripts.run_bulk_analysis \
        --uid <uid> --cohort sp500_cohort.json --store
"""

import argparse
import csv as _csv
import datetime
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import Counter
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                wait)
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

# The two guards below are private on purpose, and imported anyway rather than
# reimplemented: they are the rule that a transient 503, or a model that spent
# max_output_tokens thinking before it wrote anything, must never overwrite a
# good stored value. A batch job that quietly disagreed with collect_and_patch
# about that would be the one thing that corrupts the corpus at scale.
from data_ingestion_engine.utils.geminiProcessing import (  # noqa: E402
    _TRUNCATION_KEY, _is_error_payload, _looks_like_output, stream_gemini,
    stream_gemini_multi)
from data_ingestion_engine.utils.store_data import (  # noqa: E402
    DATAX_API_URL, fetch_reports_by_id, patch_report)

# Reused verbatim so the labels the model sees here are byte-identical to the
# ones the product sends.
from data_ingestion_engine.api.views.gemini import _document_labels  # noqa: E402

logger = logging.getLogger("run_bulk_analysis")

PROD_MARKERS = ("datax-backend-prod", "ingestion-engine.myrenx.ai")

# Model calls, not DataX reads: each one uploads PDFs and streams a long
# response, so the ceiling here is the Gemini quota, not DataX's tolerance.
DEFAULT_WORKERS = int(os.getenv("BULK_ANALYSIS_WORKERS", "4"))

_STATE_LOCK = threading.Lock()


# Set by SIGTERM/SIGINT or by the run window closing. Everything in flight is
# allowed to finish; nothing new is started. Kubernetes sends SIGTERM when a Job
# hits activeDeadlineSeconds or a node drains, so this is what turns "the day
# started, kill it" from a lost half-written unit into a clean stop.
_STOP = threading.Event()
_STOP_REASON = "stopped"


def request_stop(reason: str) -> None:
    global _STOP_REASON
    if not _STOP.is_set():
        _STOP_REASON = reason
        _STOP.set()
        logger.warning("STOPPING: %s — finishing in-flight calls, starting "
                       "nothing new", reason)


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        request_stop(f"received {signal.Signals(signum).name}")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def seconds_until(clock: str) -> float:
    """
    Seconds from now until the next occurrence of a HH:MM wall-clock time.

    Local time, deliberately: the operator schedules "stop at 06:00" against the
    clock on the wall, and the pod's TZ is what the CronJob sets.
    """
    try:
        hour, minute = (int(part) for part in clock.split(":", 1))
        target_time = datetime.time(hour, minute)
    except (ValueError, TypeError):
        raise SystemExit(f"--stop-at expects HH:MM, got {clock!r}")
    now = datetime.datetime.now()
    target = datetime.datetime.combine(now.date(), target_time)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def start_deadline_watch(seconds: float, reason: str) -> None:
    """Trip the stop flag after `seconds`. Daemon, so it never holds up exit."""
    timer = threading.Timer(seconds, request_stop, args=(reason,))
    timer.daemon = True
    timer.start()


def unit_key(unit: Dict[str, Any]) -> str:
    """Stable identity for resume state. A delta is keyed by its whole window:
    re-running after new history arrives is a different unit, not a repeat."""
    if unit["kind"] == "delta":
        window = ",".join(str(r) for r in unit["report_ids"])
        return f"earnings_delta:{unit['report_id']}:{window}"
    return f"{unit['section']}:{unit['report_id']}"


def load_state(path: str) -> Dict[str, str]:
    """{unit_key: status} from the JSONL state file, last write wins."""
    state: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return state
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("key"):
                state[row["key"]] = row.get("status", "ok")
    return state


def append_state(path: str, key: str, status: str, detail: str = "") -> None:
    with _STATE_LOCK:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "key": key, "status": status, "detail": detail[:500],
                "at": datetime.datetime.now().isoformat(timespec="seconds"),
            }) + "\n")


def collect(gen) -> tuple:
    """
    Drain a stream_gemini generator. Returns (text, truncated).

    The generator's out-of-band truncation signal is a dict, not text, and a
    hard API error arrives as a dict too — both are handled the way
    collect_and_patch handles them so the guards downstream see the same input.
    """
    aggregated = ""
    truncated = False
    for chunk in gen:
        if isinstance(chunk, dict) and chunk.get(_TRUNCATION_KEY):
            truncated = True
            continue
        if not isinstance(chunk, str):
            chunk = json.dumps(chunk)
        aggregated += chunk
    return aggregated, truncated


def should_store(text: str, truncated: bool) -> Optional[str]:
    """None if the result is safe to store, else why it is not."""
    if not text.strip():
        return "empty response"
    if _is_error_payload(text):
        return f"error payload: {text[:120]}"
    if truncated and not _looks_like_output(text):
        return (f"hit max_output_tokens before producing a report; the "
                f"{len(text)} chars received are model reasoning, not output")
    return None


ALL_SECTIONS = ("risk_analysis", "gap_analysis", "earnings_delta")


class ReportCache:
    """
    One read per REPORT, not per unit.

    Two things need the report row: the "is this section already populated?"
    check and the document URL. Naively that is a GET per unit, so a report with
    a risk unit and a gap unit is fetched twice and a delta window of five is
    fetched five times — 78k round trips over the full corpus before a single
    model call. Here each report is read once and shared.

    Only the URL and the SET OF POPULATED SECTIONS are kept. The row itself is
    not: it carries the full analysis text, which for a large company runs to
    over a megabyte, and holding tens of thousands of those would exhaust memory
    for no benefit.

    Safe because this process is the only writer during a run: a section this
    run fills is recorded locally, so a later unit sees it without re-reading.
    """

    def __init__(self, uid: str, coverage: Optional[Dict[int, set]] = None):
        self._uid = uid
        self._lock = threading.Lock()
        self._cache: Dict[int, Optional[Dict[str, Any]]] = {}
        # Populated sections from an export, so the common case needs no
        # request at all. An export lists every populated pair in the corpus,
        # so a report ABSENT from it has no analysis — which is the case that
        # matters, and the one that would otherwise cost a GET each.
        self._coverage = coverage
        self.reads = 0

    def get(self, report_id: int) -> Optional[Dict[str, Any]]:
        """{"url": ..., "populated": {section, ...}} or None when unreadable."""
        with self._lock:
            if report_id in self._cache:
                return self._cache[report_id]

        row = fetch_reports_by_id(report_id, self._uid)
        entry = None
        if row:
            populated = {s for s in ALL_SECTIONS
                         if row.get(s) and str(row[s]).strip()}
            entry = {"url": row.get("document_url"), "populated": populated}

        with self._lock:
            self.reads += 1
            self._cache[report_id] = entry
            return entry

    def is_populated(self, report_id: int, section: str) -> bool:
        """
        True when this section already holds a value.

        The coverage export answers first and costs nothing, which is what
        removes the bulk of the reads: over a mostly-analysed corpus, most units
        are skipped without a request. What the export does not vouch for is
        confirmed against DataX.
        """
        # The export is trusted in ONE direction only. "Already populated" is
        # safe to believe: the worst case is skipping work that was somehow
        # undone, which costs nothing but a re-run. "Not populated" is NOT safe
        # to believe — an export taken last week, or a query that missed rows,
        # would licence overwriting analysis that exists now. So a miss falls
        # through to a live read, which is also what stops us paying for a model
        # call whose result we would then refuse to store.
        if self._coverage and section in self._coverage.get(report_id, ()):
            return True
        entry = self.get(report_id)
        return bool(entry and section in entry["populated"])

    def refresh(self, report_id: int) -> Optional[Dict[str, Any]]:
        """
        Re-read a report, ignoring the cache.

        Used immediately before a write. A model call takes minutes, and the
        cached view of "empty" was taken before it started — this is what makes
        the guarantee hold against anything else that wrote in the meantime,
        including another shard of this same job.
        """
        with self._lock:
            self._cache.pop(report_id, None)
        return self.get(report_id)

    def mark(self, report_id: int, section: str) -> None:
        """Record a section this run just filled."""
        with self._lock:
            entry = self._cache.get(report_id)
            if entry:
                entry["populated"].add(section)


def load_coverage(path: str) -> Dict[int, set]:
    """
    Which (report_id, section) pairs are ALREADY populated, exported from the
    DataX database.

    Turns the skip check from a per-report request into a dictionary lookup,
    which is what makes a full-corpus run practical: the alternative is tens of
    thousands of GETs before any work starts.

    Accepts a CSV with a `report_id` column plus either a `section` column (one
    row per populated pair) or one boolean-ish column per section named
    `risk_analysis` / `gap_analysis` / `earnings_delta`.
    """
    coverage: Dict[int, set] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = _csv.DictReader(handle)
        columns = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        id_column = next((columns[c] for c in ("report_id", "id") if c in columns),
                         None)
        if not id_column:
            raise SystemExit(
                f"{path}: no report_id column found (saw {reader.fieldnames})")
        section_column = columns.get("section")
        flag_columns = {s: columns[s] for s in ALL_SECTIONS if s in columns}
        if not section_column and not flag_columns:
            raise SystemExit(
                f"{path}: needs either a `section` column or one column per "
                f"section named {', '.join(ALL_SECTIONS)}")

        truthy = {"1", "true", "t", "yes", "y"}
        for row in reader:
            raw = str(row.get(id_column) or "").strip()
            try:
                report_id = int(float(raw))
            except (TypeError, ValueError):
                continue
            populated = coverage.setdefault(report_id, set())
            if section_column:
                section = str(row.get(section_column) or "").strip()
                if section in ALL_SECTIONS:
                    populated.add(section)
            for section, column in flag_columns.items():
                if str(row.get(column) or "").strip().lower() in truthy:
                    populated.add(section)

    return coverage


def run_unit(unit: Dict[str, Any], uid: str, store: bool,
             overwrite: bool, cache: "ReportCache") -> Dict[str, Any]:
    """
    One model call. Returns a result row; never raises.

    Unless `overwrite` is set, a section that already holds analysis is never
    touched: it is checked before the model call (so no money is spent) and
    again against a fresh read before the write (so nothing is replaced).
    """
    key = unit_key(unit)
    started = time.time()
    result = {
        "key": key,
        "kind": unit["kind"],
        "section": unit["section"],
        "company_id": unit.get("company_id"),
        "report_id": unit["report_id"],
        "status": "ok",
        "detail": "",
        "chars": 0,
        "seconds": 0.0,
    }
    try:
        if not overwrite and cache.is_populated(unit["report_id"], unit["section"]):
            result["status"] = "skipped"
            result["detail"] = "section already populated"
            return result

        if unit["kind"] == "delta":
            # Periods come from the cohort; only the URLs are read. Falls back
            # to the report row for a cohort built before `window` existed.
            window = unit.get("window") or [{"report_id": r}
                                            for r in unit["report_ids"]]
            docs = []
            for item in window:
                entry = cache.get(item["report_id"])
                if not entry or not entry["url"]:
                    result["status"] = "failed"
                    result["detail"] = (f"report {item['report_id']} missing or "
                                        f"has no URL")
                    return result
                docs.append({"year": item.get("year"),
                             "quarter": item.get("quarter"),
                             "url": entry["url"]})
            documents = [{"label": label, "url": doc["url"]}
                         for label, doc in zip(_document_labels(docs), docs)]
            text, truncated = collect(
                stream_gemini_multi("earnings_delta", documents))
        else:
            entry = cache.get(unit["report_id"])
            if not entry or not entry["url"]:
                result["status"] = "failed"
                result["detail"] = "report missing or has no URL"
                return result
            text, truncated = collect(
                stream_gemini(unit["section"], entry["url"]))

        result["chars"] = len(text)
        result["text"] = text

        refusal = should_store(text, truncated)
        if refusal:
            result["status"] = "failed"
            result["detail"] = refusal
            return result

        if store:
            # Last check before writing, against a FRESH read. patch_report is a
            # blind PATCH: it replaces whatever is there. The emptiness we
            # checked before the model call is minutes stale by now, and this
            # job may be running as several shards — so the value is confirmed
            # still empty here, immediately before it is replaced.
            if not overwrite:
                entry = cache.refresh(unit["report_id"])
                if entry and unit["section"] in entry["populated"]:
                    result["status"] = "skipped"
                    result["detail"] = ("populated while this call was running — "
                                        "NOT overwritten; the generated text is "
                                        "in the results file")
                    return result
            if not patch_report(unit["report_id"], text, unit["section"], uid):
                result["status"] = "failed"
                result["detail"] = "patch_report returned False"
                return result
            cache.mark(unit["report_id"], unit["section"])
            result["detail"] = "stored"
        else:
            result["detail"] = "local only (no --store)"

    except Exception as e:
        result["status"] = "failed"
        result["detail"] = f"{type(e).__name__}: {e}"
    finally:
        result["seconds"] = round(time.time() - started, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Run the bulk analyses over a cohort. Writes are opt-in.")
    # Defaulted from the environment so a CronJob can supply it through
    # envFrom rather than carrying it in the pod's command line.
    parser.add_argument("--uid", default=os.getenv("BULK_ANALYSIS_UID"),
                        help="DataX uid (env: BULK_ANALYSIS_UID)")
    parser.add_argument("--cohort", required=True,
                        help="JSON from build_analysis_cohort.py")
    parser.add_argument("--sections",
                        help="comma-separated subset, e.g. risk_analysis,"
                             "earnings_delta (default: everything in the cohort)")
    parser.add_argument("--company-ids",
                        help="restrict to these company ids, e.g. 25,58")
    parser.add_argument("--limit", type=int,
                        help="run only the first N units — use this first")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--plan", action="store_true",
                        help="print the calls that would run; NO model calls")
    parser.add_argument("--store", action="store_true",
                        help="persist each result, REPLACING the stored section")
    parser.add_argument("--coverage",
                        help="CSV of already-populated (report_id, section) "
                             "pairs exported from the DataX database; makes "
                             "the skip check free instead of one GET per report")
    parser.add_argument("--overwrite", action="store_true",
                        help="DESTRUCTIVE: re-run and REPLACE sections that "
                             "already hold analysis. Off by default — existing "
                             "analysis is never touched")
    parser.add_argument("--out", default="bulk_analysis_results.json")
    parser.add_argument("--out-csv")
    parser.add_argument("--state",
                        help="resume state JSONL (default: <out>.state.jsonl)")
    parser.add_argument("--stop-at",
                        help="wall-clock HH:MM to stop starting new calls, e.g. "
                             "06:00 for a night-only run. In-flight calls "
                             "finish; the rest resume from the state file")
    parser.add_argument("--max-runtime", type=float,
                        help="same, expressed as seconds from now")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt for --store")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if not args.uid:
        raise SystemExit("--uid is required (or set BULK_ANALYSIS_UID)")

    with open(args.cohort, encoding="utf-8") as handle:
        cohort = json.load(handle)

    wanted_sections = ({s.strip() for s in args.sections.split(",") if s.strip()}
                       if args.sections else None)
    wanted_companies = ({int(c) for c in args.company_ids.split(",") if c.strip()}
                        if args.company_ids else None)

    units: List[Dict[str, Any]] = []
    for entry in cohort.get("companies", []):
        if wanted_companies and entry["company_id"] not in wanted_companies:
            continue
        for unit in entry.get("work_units", []):
            if wanted_sections and unit["section"] not in wanted_sections:
                continue
            unit = dict(unit)
            unit.setdefault("company_id", entry["company_id"])
            unit["company_name"] = entry.get("company_name")
            units.append(unit)

    state_path = args.state or f"{args.out}.state.jsonl"
    state = load_state(state_path)
    done = {k for k, status in state.items() if status in ("ok", "skipped")}
    pending = [u for u in units if unit_key(u) not in done]

    # Pre-filter against the export rather than letting run_unit skip them one
    # by one. Over a full corpus most units are already done, and a worker slot
    # spent returning "skipped" is a slot not spent on a real call — and with
    # --limit N it would mean N skips and no work.
    coverage = None
    if args.coverage:
        if os.path.exists(args.coverage):
            coverage = load_coverage(args.coverage)
        else:
            # Degrade rather than die. The scheduled job passes --coverage
            # unconditionally, and a missing export means a slower run — one
            # read per report instead of a lookup — not a wasted night.
            logger.warning("coverage file %s does not exist — falling back to "
                           "one read per report for the skip check",
                           args.coverage)
    already_populated = 0
    if coverage is not None and not args.overwrite:
        before = len(pending)
        pending = [u for u in pending
                   if u["section"] not in coverage.get(u["report_id"], ())]
        already_populated = before - len(pending)

    if args.limit:
        pending = pending[:args.limit]

    by_section = Counter(u["section"] for u in pending)
    print()
    print(f"DataX          {DATAX_API_URL}")
    print(f"cohort         {args.cohort} "
          f"({cohort.get('start_year')}-{cohort.get('end_year')})")
    already_done = sum(1 for u in units if unit_key(u) in done)
    print(f"units total    {len(units)}")
    print(f"  already done {already_done}")
    if coverage is not None:
        print(f"  populated    {already_populated} (from {args.coverage})")
    print(f"  to run       {len(pending)}")
    for section, count in by_section.most_common():
        print(f"    {section:<22}{count}")
    if not args.store:
        writes = "off (local JSON only)"
    elif args.overwrite:
        writes = "ON — REPLACES sections that already hold analysis"
    else:
        writes = "ON — fills EMPTY sections only, never overwrites"
    print(f"writes         {writes}")
    print()

    if args.plan:
        for unit in pending[:50]:
            target = (f"window {unit['periods']}" if unit["kind"] == "delta"
                      else f"{unit['document_type']} {unit.get('quarter','')} {unit.get('year','')}")
            print(f"  {unit['section']:<20} company {unit['company_id']:<7} "
                  f"report {unit['report_id']:<9} {target}")
        if len(pending) > 50:
            print(f"  ... and {len(pending) - 50} more")
        print("\nPlan only — nothing was sent to the model.")
        return 0

    if not pending:
        print("Nothing to do.")
        return 0

    if args.store:
        is_prod = any(marker in DATAX_API_URL for marker in PROD_MARKERS)
        reports = len({u["report_id"] for u in pending})
        where = " on PRODUCTION" if is_prod else ""
        if args.overwrite:
            print(f"About to run {len(pending)} model calls and REPLACE the "
                  f"stored section on {reports} reports{where}, INCLUDING ones "
                  f"that already hold analysis.")
        else:
            print(f"About to run {len(pending)} model calls and fill EMPTY "
                  f"sections on up to {reports} reports{where}. Sections that "
                  f"already hold analysis are skipped, not replaced.")
        if not args.yes:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                print("Aborted.")
                return 1

    install_signal_handlers()
    if args.stop_at:
        remaining = seconds_until(args.stop_at)
        start_deadline_watch(remaining, f"reached --stop-at {args.stop_at}")
        logger.info("run window closes at %s, in %.1f hours",
                    args.stop_at, remaining / 3600)
    if args.max_runtime:
        start_deadline_watch(args.max_runtime,
                             f"reached --max-runtime {args.max_runtime}s")

    results: List[Dict[str, Any]] = []
    counts: Counter = Counter()
    cache = ReportCache(args.uid, coverage)
    workers = max(1, args.workers)
    queued = iter(pending)
    index = 0
    # Submitted lazily, a couple of units deep per worker, rather than all at
    # once. Handing the pool 78,000 futures up front would mean a stop request
    # could only be honoured by having every one of them wake up and decline —
    # here the loop simply stops feeding it.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight: Dict[Any, Dict[str, Any]] = {}
        while True:
            while len(inflight) < workers * 2 and not _STOP.is_set():
                try:
                    unit = next(queued)
                except StopIteration:
                    break
                inflight[pool.submit(run_unit, unit, args.uid, args.store,
                                     args.overwrite, cache)] = unit
            if not inflight:
                break
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for future in done:
                unit = inflight.pop(future)
                index += 1
                result = future.result()
                result["company_name"] = unit.get("company_name")
                counts[result["status"]] += 1
                append_state(state_path, result["key"], result["status"],
                             result["detail"])
                results.append(result)
                logger.info("[%s/%s] %s %s report %s -> %s (%ss) %s",
                            index, len(pending), result["status"].upper(),
                            result["section"], result["report_id"],
                            f"{result['chars']} chars", result["seconds"],
                            result["detail"])

    stopped_early = _STOP.is_set()

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"datax_api_url": DATAX_API_URL,
                   "stored": args.store,
                   "results": results}, handle, indent=2)

    if args.out_csv:
        fields = ["key", "company_id", "company_name", "report_id", "section",
                  "kind", "status", "chars", "seconds", "detail"]
        with open(args.out_csv, "w", newline="", encoding="utf-8") as handle:
            writer = _csv.DictWriter(handle, fieldnames=fields,
                                     extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)

    print()
    for status, count in counts.most_common():
        print(f"  {status:<10}{count}")
    print(f"  report reads {cache.reads}")
    if stopped_early:
        remaining_units = len(pending) - len(results)
        print(f"  not started  {remaining_units} ({_STOP_REASON})")
    print(f"wrote {args.out}" + (f" and {args.out_csv}" if args.out_csv else ""))
    print(f"resume state in {state_path}")
    if not args.store:
        print("Nothing was written to DataX — re-run with --store to persist.")
    if stopped_early:
        # Exit 0: stopping at the end of the run window is the design, not a
        # failure. A CronJob that saw a non-zero code here would report a failed
        # Job every single morning.
        print(f"Stopped early ({_STOP_REASON}). Re-run the same command to "
              f"continue from {state_path}.")
        return 0
    return 0 if not counts["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
