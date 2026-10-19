#!/usr/bin/env python
"""
Build the cohort for the bulk risk / gap / earnings-delta run. NO model calls.

GETs only, so this is free and safe to re-run. It answers, before a penny is
spent: which companies, which documents, and exactly how many model calls the
run implies.

  IMPORTANT — there is no S&P 500 in DataX. The internal API exposes
  /companies/ids and /companies/{id}; nothing carries index membership. What the
  --exchange / --country filters give you is "companies in DataX listed on those
  exchanges", which is a superset of the S&P 500 and will not match it. To get
  the actual index, pass --tickers with a constituent list and the cohort is
  intersected against it. Read the printed counts before running anything.

Output is one JSON (the machine input for run_bulk_analysis.py) plus a CSV for
eyeballing. The JSON records the exact work units, so the run step never has to
re-decide what to work on.

Usage:
    # what the universe looks like, and what it would cost
    python -m data_ingestion_engine.scripts.build_analysis_cohort \
        --uid <uid> --exchange NYSE,NASDAQ \
        --out sp500_cohort.json --out-csv sp500_cohort.csv

    # the real index, from a constituent list
    python -m data_ingestion_engine.scripts.build_analysis_cohort \
        --uid <uid> --tickers sp500.csv --out sp500_cohort.json
"""

import argparse
import csv as _csv
import json
import logging
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

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

from data_ingestion_engine.utils.bulk_analysis import (  # noqa: E402
    IN_SCOPE_TYPES, build_work_units, default_end_year, normalise_documents,
    unflatten_envelope)
from data_ingestion_engine.utils.store_data import (  # noqa: E402
    DATAX_API_URL, DATAX_INTERNAL, _headers, _session, fetch_company,
    fetch_document_metadata, fetch_reports_for_company)

logger = logging.getLogger("build_analysis_cohort")

# Short DataX reads, so the ceiling is DataX's tolerance rather than ours.
WORKERS = int(os.getenv("COHORT_WORKERS", "12"))

CSV_FIELDS = ["company_id", "company_name", "symbol", "exchange", "country",
              "sector", "documents", "transcripts", "doc_calls", "delta_calls",
              "types", "years"]


def _first(row: Dict[str, Any], *keys: str) -> Optional[str]:
    """DataX company rows have moved key names around; accept the aliases."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def list_company_ids(uid: str, page_size: int = 200) -> List[int]:
    """Every company id that HAS documents. Paginated; ids only, so it is cheap."""
    ids: List[int] = []
    page = 1
    while True:
        url = (f"{DATAX_INTERNAL}/companies/ids?with_docs=true"
               f"&page={page}&page_size={page_size}")
        with _session() as s:
            resp = s.get(url, headers=_headers(uid), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        ids.extend(data.get("items", []))
        total_pages = data.get("total_pages", 0)
        logger.info("companies/ids page %s/%s (%s ids so far)",
                    page, total_pages, len(ids))
        if page >= total_pages:
            break
        page += 1
    return ids


def load_tickers(path: str) -> Set[str]:
    """Constituent symbols from a CSV. Any column named symbol/ticker is read."""
    symbols: Set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = _csv.DictReader(handle)
        columns = [c for c in (reader.fieldnames or [])
                   if c and c.strip().lower() in
                   ("symbol", "ticker", "ticker symbol", "tickers")]
        if not columns:
            raise SystemExit(
                f"{path}: no symbol/ticker column found "
                f"(saw {reader.fieldnames})")
        for row in reader:
            for column in columns:
                for part in str(row.get(column) or "").split(","):
                    part = part.strip().upper()
                    if part:
                        symbols.add(part)
    return symbols


def load_companies_csv(path: str) -> List[Dict[str, Any]]:
    """
    A company list exported straight from the DataX database.

    This is the preferred input. The universe scan exists only because the API
    exposes ids and per-company rows separately, so it costs one GET per company
    to learn an exchange; an export costs one file read and can also carry
    columns the internal API never returns.

    Only `company_id` is required — everything else is carried through for the
    audit CSV. Ids that are blank or non-numeric are reported and skipped rather
    than guessed at.
    """
    companies: List[Dict[str, Any]] = []
    skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = _csv.DictReader(handle)
        columns = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        id_column = next((columns[c] for c in ("company_id", "id", "companyid")
                          if c in columns), None)
        if not id_column:
            raise SystemExit(
                f"{path}: no company_id column found (saw {reader.fieldnames})")

        def pick(row, *names):
            for name in names:
                column = columns.get(name)
                if column and str(row.get(column) or "").strip():
                    return str(row[column]).strip()
            return None

        seen: Set[int] = set()
        for row in reader:
            raw = str(row.get(id_column) or "").strip()
            try:
                # Tolerates "25.0", which is what a spreadsheet round-trip does
                # to an integer column.
                company_id = int(float(raw))
            except (TypeError, ValueError):
                skipped += 1
                continue
            if company_id in seen:
                continue
            seen.add(company_id)
            companies.append({
                "company_id": company_id,
                "company_name": pick(row, "company_name", "name"),
                "symbol": pick(row, "symbol", "ticker", "ticker_str", "tickers") or "",
                "exchange": (pick(row, "exchange") or "").upper(),
                "country": (pick(row, "country") or "").upper(),
                "sector": pick(row, "sector"),
            })
    if skipped:
        logger.warning("%s rows in %s had no usable company_id and were skipped",
                       skipped, path)
    return companies


def _symbols_of(company: Dict[str, Any]) -> Set[str]:
    raw = _first(company, "symbol", "ticker", "ticker_str", "tickers") or ""
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def select_companies(
    uid: str,
    exchanges: Optional[Set[str]],
    countries: Optional[Set[str]],
    tickers: Optional[Set[str]],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    """
    Company rows matching the filters.

    Every id has to be read individually: /companies/ids returns ids only and
    there is no server-side exchange filter, so the exchange lives on
    /companies/{id}. Parallel, but it is still one GET per company — expect a
    few minutes over a large corpus, and re-use the output rather than
    rebuilding it each run.
    """
    ids = list_company_ids(uid)
    logger.info("%s companies with documents", len(ids))

    selected: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_company, cid, uid): cid for cid in ids}
        for future in as_completed(futures):
            company = future.result()
            if not company:
                continue
            company_id = futures[future]
            exchange = (_first(company, "exchange") or "").upper()
            country = (_first(company, "country") or "").upper()
            symbols = _symbols_of(company)

            if exchanges and exchange not in exchanges:
                continue
            if countries and country not in countries:
                continue
            if tickers and not (symbols & tickers):
                continue

            selected.append({
                "company_id": company_id,
                "company_name": _first(company, "company_name", "name"),
                "symbol": ", ".join(sorted(symbols)),
                "exchange": exchange,
                "country": country,
                "sector": _first(company, "sector"),
            })

    selected.sort(key=lambda c: (c["company_name"] or "", c["company_id"]))
    if limit:
        selected = selected[:limit]
    return selected


def fetch_documents(company_ids: List[int], uid: str,
                    year_from: int, year_to: int) -> Dict[int, List[Dict[str, Any]]]:
    """
    Documents for every company, grouped by company_id.

    Preferred path is the bulk metadata endpoint: 8 columns in SQL and up to 200
    ids per request, so a 500-company cohort is 3 round trips rather than 500.
    It returns None when it is not deployed, in which case we fall back to the
    per-company route.
    """
    by_company: Dict[int, List[Dict[str, Any]]] = {cid: [] for cid in company_ids}

    rows = fetch_document_metadata(company_ids, uid,
                                   year_from=year_from, year_to=year_to)
    if rows is not None:
        for row in rows:
            cid = row.get("company_id")
            if cid in by_company:
                by_company[cid].append(row)
        return by_company

    logger.info("bulk metadata endpoint unavailable — falling back to "
                "per-company reads for %s companies", len(company_ids))

    def fetch(cid: int):
        payload = fetch_reports_for_company(cid, uid)
        return cid, unflatten_envelope(payload, cid)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for future in as_completed([pool.submit(fetch, c) for c in company_ids]):
            try:
                cid, found = future.result()
            except Exception as e:
                logger.warning("could not read documents for a company: %s", e)
                continue
            by_company[cid] = found
    return by_company


def resolve_symbol_duplicates(
    entries: List[Dict[str, Any]],
) -> "tuple":
    """
    Collapse companies that share a ticker down to one id each.

    DataX holds duplicate company rows: the same ticker appears under two
    canonical ids — AT&T is 169 and 253, Atlassian 260 and 2879 — and 83 symbols
    in the current NYSE/NASDAQ export do this. Analysing both means paying twice
    for one company, or paying once for an empty twin that scores nothing.

    Resolved the same way resolve_company_cohort.py resolves a match: the id
    holding DOCUMENTS wins, because a resolution that "succeeds" is not the same
    as a resolution that is right. Ties — both ids carrying documents — are kept
    on the larger id and REPORTED, because that is a genuine ambiguity a person
    should look at, not something to silently pick a winner for.

    Returns (kept_entries, dropped_entries, ambiguous_symbols).
    """
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    unkeyed: List[Dict[str, Any]] = []
    for entry in entries:
        symbols = sorted({s.strip().upper()
                          for s in (entry.get("symbol") or "").split(",")
                          if s.strip()})
        if not symbols:
            # No ticker to collide on; nothing to decide.
            unkeyed.append(entry)
            continue
        by_symbol.setdefault(", ".join(symbols), []).append(entry)

    kept = list(unkeyed)
    dropped: List[Dict[str, Any]] = []
    ambiguous: List[str] = []

    for symbol, group in by_symbol.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=lambda e: (e["counts"]["documents"], e["company_id"]),
                   reverse=True)
        winner, losers = group[0], group[1:]
        if sum(1 for e in group if e["counts"]["documents"]) > 1:
            ambiguous.append(symbol)
        kept.append(winner)
        for loser in losers:
            dropped.append({
                "symbol": symbol,
                "company_id": loser["company_id"],
                "company_name": loser["company_name"],
                "documents": loser["counts"]["documents"],
                "kept_company_id": winner["company_id"],
                "kept_documents": winner["counts"]["documents"],
            })

    kept.sort(key=lambda e: (e["company_name"] or "", e["company_id"]))
    return kept, dropped, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Resolve the company universe and the work it implies. "
                    "No model calls.")
    # Defaulted from the environment so a CronJob can supply it through
    # envFrom rather than carrying it in the pod's command line.
    parser.add_argument("--uid", default=os.getenv("BULK_ANALYSIS_UID"),
                        help="DataX uid (env: BULK_ANALYSIS_UID)")
    parser.add_argument("--exchange",
                        help="comma-separated, e.g. NYSE,NASDAQ")
    parser.add_argument("--country",
                        help="comma-separated, matched case-insensitively")
    parser.add_argument("--tickers",
                        help="CSV of index constituents; the cohort is "
                             "intersected against its symbol/ticker column")
    parser.add_argument("--companies-csv",
                        help="company list exported from the DataX database; "
                             "needs a company_id column. Preferred input — "
                             "skips the universe scan entirely")
    parser.add_argument("--company-ids",
                        help="explicit ids, e.g. 25,58 — skips the universe scan")
    parser.add_argument("--years", type=int, default=2,
                        help="how many reporting years are IN SCOPE (default 2)")
    parser.add_argument("--end-year", type=int, default=default_end_year(),
                        help="most recent in-scope reporting year")
    parser.add_argument("--history-years", type=int, default=1,
                        help="extra earlier years fetched as earnings-delta "
                             "BASELINES only; never analysed themselves")
    parser.add_argument("--no-delta", action="store_true",
                        help="skip earnings delta entirely")
    parser.add_argument("--keep-duplicates", action="store_true",
                        help="keep every company sharing a ticker (default: "
                             "collapse to the id holding documents)")
    parser.add_argument("--limit", type=int, help="first N companies")
    parser.add_argument("--out", default="analysis_cohort.json")
    parser.add_argument("--out-csv")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if not args.uid:
        raise SystemExit("--uid is required (or set BULK_ANALYSIS_UID)")

    end_year = args.end_year
    start_year = end_year - args.years + 1
    fetch_from = start_year - max(0, args.history_years)
    logger.info("DataX: %s", DATAX_API_URL)
    logger.info("in scope %s-%s, fetching from %s for delta baselines",
                start_year, end_year, fetch_from)

    if args.companies_csv:
        companies = load_companies_csv(args.companies_csv)
        before = len(companies)
        # The same filters as the scan path. An export is typically wider than
        # what you want to run — the DataX export is every NYSE/NASDAQ company,
        # 30% of which are not US-domiciled — so these have to bite here too.
        if args.exchange:
            exchanges = {e.strip().upper() for e in args.exchange.split(",") if e.strip()}
            companies = [c for c in companies if (c["exchange"] or "") in exchanges]
        if args.country:
            countries = {c.strip().upper() for c in args.country.split(",") if c.strip()}
            companies = [c for c in companies if (c["country"] or "") in countries]
        if args.tickers:
            wanted_symbols = load_tickers(args.tickers)
            companies = [c for c in companies
                         if {s.strip().upper()
                             for s in (c["symbol"] or "").split(",") if s.strip()}
                         & wanted_symbols]
        logger.info("%s companies in %s, %s after filters",
                    before, args.companies_csv, len(companies))
        if args.limit:
            companies = companies[:args.limit]
    elif args.company_ids:
        ids = [int(c) for c in args.company_ids.split(",") if c.strip()]
        companies = []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for cid, company in zip(ids, pool.map(
                    lambda c: fetch_company(c, args.uid), ids)):
                companies.append({
                    "company_id": cid,
                    "company_name": _first(company or {}, "company_name", "name"),
                    "symbol": ", ".join(sorted(_symbols_of(company or {}))),
                    "exchange": _first(company or {}, "exchange"),
                    "country": _first(company or {}, "country"),
                    "sector": _first(company or {}, "sector"),
                })
    else:
        exchanges = ({e.strip().upper() for e in args.exchange.split(",") if e.strip()}
                     if args.exchange else None)
        countries = ({c.strip().upper() for c in args.country.split(",") if c.strip()}
                     if args.country else None)
        tickers = load_tickers(args.tickers) if args.tickers else None
        if not (exchanges or countries or tickers):
            logger.warning("no --exchange/--country/--tickers filter given — "
                           "this selects EVERY company in DataX that has "
                           "documents")
        companies = select_companies(args.uid, exchanges, countries, tickers,
                                     args.limit)

    logger.info("%s companies selected", len(companies))
    if not companies:
        print("Nothing selected — check the filters.", file=sys.stderr)
        return 1

    if args.tickers:
        wanted = load_tickers(args.tickers)
        matched = set()
        for company in companies:
            matched |= {s.strip().upper()
                        for s in (company["symbol"] or "").split(",") if s.strip()}
        missing = sorted(wanted - matched)
        if missing:
            logger.warning("%s of %s constituent symbols matched no company in "
                           "DataX: %s%s", len(missing), len(wanted),
                           ", ".join(missing[:20]),
                           " ..." if len(missing) > 20 else "")

    company_ids = [c["company_id"] for c in companies]
    documents_by_company = fetch_documents(company_ids, args.uid,
                                           fetch_from, end_year)

    entries = []
    for company in companies:
        cid = company["company_id"]
        documents = normalise_documents(documents_by_company.get(cid, []))
        units = build_work_units(documents, start_year, end_year,
                                 include_delta=not args.no_delta)
        in_scope = [d for d in documents
                    if d["year"] is not None and start_year <= d["year"] <= end_year]
        transcripts = [d for d in in_scope
                       if d["document_type"] == "Earnings Call Transcript"]
        entries.append({
            **company,
            "documents": in_scope,
            "work_units": units,
            "counts": {
                "documents": len(in_scope),
                "transcripts": len(transcripts),
                "doc_calls": sum(1 for u in units if u["kind"] == "document"),
                "delta_calls": sum(1 for u in units if u["kind"] == "delta"),
            },
        })

    # After the document counts exist, not before: which duplicate wins is
    # decided by which one actually holds documents.
    dropped_duplicates: List[Dict[str, Any]] = []
    ambiguous: List[str] = []
    if args.keep_duplicates:
        logger.warning("--keep-duplicates: companies sharing a ticker are BOTH "
                       "kept, so a duplicated company is paid for twice")
    else:
        entries, dropped_duplicates, ambiguous = resolve_symbol_duplicates(entries)

    type_counts: Counter = Counter()
    doc_calls = delta_calls = 0
    no_documents = []
    for entry in entries:
        for doc in entry["documents"]:
            type_counts[doc["document_type"]] += 1
        doc_calls += entry["counts"]["doc_calls"]
        delta_calls += entry["counts"]["delta_calls"]
        if not entry["documents"]:
            no_documents.append(entry)

    cohort = {
        "datax_api_url": DATAX_API_URL,
        "start_year": start_year,
        "end_year": end_year,
        "history_from": fetch_from,
        "in_scope_types": list(IN_SCOPE_TYPES),
        "sections": ["risk_analysis", "gap_analysis"] +
                    ([] if args.no_delta else ["earnings_delta"]),
        "dropped_duplicates": dropped_duplicates,
        "ambiguous_duplicates": ambiguous,
        "companies": entries,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(cohort, handle, indent=2)

    if args.out_csv:
        with open(args.out_csv, "w", newline="", encoding="utf-8") as handle:
            writer = _csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for entry in entries:
                docs = entry["documents"]
                writer.writerow({
                    "company_id": entry["company_id"],
                    "company_name": entry["company_name"],
                    "symbol": entry["symbol"],
                    "exchange": entry["exchange"],
                    "country": entry["country"],
                    "sector": entry["sector"],
                    "documents": entry["counts"]["documents"],
                    "transcripts": entry["counts"]["transcripts"],
                    "doc_calls": entry["counts"]["doc_calls"],
                    "delta_calls": entry["counts"]["delta_calls"],
                    "types": ", ".join(sorted({d["document_type"] for d in docs})),
                    "years": ", ".join(str(y) for y in sorted(
                        {d["year"] for d in docs if d["year"] is not None})),
                })

    print()
    print(f"companies selected      {len(entries)}")
    print(f"  with no in-scope docs {len(no_documents)}")
    if dropped_duplicates:
        print(f"  duplicate ids dropped {len(dropped_duplicates)}")
    print(f"in-scope documents      {sum(type_counts.values())}")
    for label, count in type_counts.most_common():
        print(f"  {label:<26}{count}")
    print()
    print(f"MODEL CALLS the run implies")
    print(f"  risk + gap            {doc_calls}")
    print(f"  earnings delta        {delta_calls}")
    print(f"  total                 {doc_calls + delta_calls}")

    if ambiguous:
        print()
        print(f"!! {len(ambiguous)} tickers have TWO company ids that BOTH hold "
              f"documents — the larger id was kept, check these by hand:")
        for symbol in ambiguous[:15]:
            row = next(d for d in dropped_duplicates if d["symbol"] == symbol)
            print(f"   {symbol:<10} kept {row['kept_company_id']} "
                  f"({row['kept_documents']} docs), dropped {row['company_id']} "
                  f"({row['documents']} docs)")
        if len(ambiguous) > 15:
            print(f"   ... and {len(ambiguous) - 15} more, all in {args.out}")

    # An index has ~500 members. A cohort an order of magnitude larger is the
    # exchange superset, and running it is an order of magnitude more spend than
    # was intended — so say so here, where it is still free to change course.
    if len(entries) > 1000 and not args.tickers:
        print()
        print(f"!! {len(entries)} companies is NOT an index — DataX does not "
              f"record index membership, so this is every company matching the "
              f"exchange/country filters.")
        print(f"   Pass --tickers <constituents.csv> to cut it to the real "
              f"index, or accept {doc_calls + delta_calls} model calls.")

    print()
    print(f"wrote {args.out}" + (f" and {args.out_csv}" if args.out_csv else ""))
    print("Nothing was sent to the model and nothing was written to DataX.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
