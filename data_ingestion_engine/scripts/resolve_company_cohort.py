#!/usr/bin/env python
"""
Resolve a name/ticker company list to canonical company ids. No model calls.

The progressive endpoint takes canonical ids and deliberately does no
resolution: a leaderboard export already carries them, and re-resolving risks
landing on the duplicate empty row that the ISIN-only strategy exists to avoid.
A list of company NAMES and TICKERS has no ids, so resolution has to happen
first, separately, and be reviewed before any model call is spent.

Why this is its own gated step rather than something the endpoint does inline:
the canonical table holds duplicate companies, and a resolution that "succeeds"
is not the same as a resolution that is right. AbbVie resolves to id 29351 with
ticker and exchange attached — a real row, with zero documents — and to id 25,
which holds 244, on ISIN alone. Every candidate here is therefore checked for
documents before it is accepted, and a match with none is reported rather than
returned.

This cohort has no ISIN, which is the strongest identifier available and the one
the measured strategies are built around. Resolution runs on name and ticker
instead, so the unresolved and no-documents lists are expected to be longer than
they would be for an ISIN-bearing sheet. Read them.

Strategies, in order, first one holding documents wins:

  1. name+ticker      DataX's own matching, ticker included
  2. name             DataX's matching on the name alone — ticker omitted for
                      the same reason ISIN alone beats ISIN+ticker: extra
                      identifiers can steer matching onto the wrong twin
  3. name search      /search/v2, accepting only an exact normalised-name match,
                      then the whole same-name group's documents

Usage:
    python -m data_ingestion_engine.scripts.resolve_company_cohort \
        --uid <uid> --api-url https://<prod-datax-host> \
        --csv "DGI List - Companies to Add.csv" \
        --out dgi_resolved.json --out-csv dgi_resolved.csv
"""

import argparse
import csv as _csv
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

from data_ingestion_engine.utils.ren.company_group import (  # noqa: E402
    find_companies_by_name, normalise_company_name)
from data_ingestion_engine.utils.ren.document_selection import (  # noqa: E402
    select_all_ren_documents)
from data_ingestion_engine.utils.store_data import (  # noqa: E402
    fetch_company, resolve_company_id)

logger = logging.getLogger("resolve_company_cohort")

# Short DataX reads, so the ceiling is DataX's tolerance rather than ours.
WORKERS = int(os.getenv("RESOLVE_WORKERS", "12"))

REPORT_FIELDS = ["row", "company", "ticker", "company_id", "strategy",
                 "matched_name", "documents", "company_years", "years",
                 "status", "detail"]


# Legal forms a source list commonly omits. Only a BARE suffix is accepted as
# the difference between a source name and a canonical one — anything longer is
# a different company. normalise_company_name() deliberately keeps suffixes so a
# parent never merges with its subsidiary, and nothing here relaxes that.
_LEGAL_SUFFIXES = (
    "plc", "ltd", "limited", "inc", "incorporated", "corp", "corporation",
    "co", "company", "sa", "nv", "ag", "se", "spa", "ab", "asa", "oyj", "oy",
    "as", "bv", "gmbh", "kgaa", "sas", "srl", "aps", "aktiengesellschaft",
)


def _suffix_variants(name: str):
    """[(strategy_label, name_to_search), ...] — the bare name first."""
    yield "name search", name
    for suffix in _LEGAL_SUFFIXES:
        yield f"name+{suffix}", f"{name} {suffix}"


# Tokens that carry no identity. Dropping them lets "Lowe's Cos Inc" and
# "Lowe's Companies, Inc." compare equal without loosening anything that
# matters: what remains on each side still has to match exactly.
#
# "international" is NOT here — "Babcock International" and "Babcock" are
# different companies, and so are "Intl Consolidated Airlines" and anything
# else. Abbreviations of it are expanded rather than dropped.
_NOISE_TOKENS = frozenset((
    "the", "and", "of", "co", "cos", "company", "companies", "corp", "corps",
    "corporation", "inc", "incorporated", "plc", "ltd", "limited", "lp", "llc",
    "group", "groups", "holding", "holdings", "class", "common", "stock",
    "sa", "nv", "ag", "se", "spa", "ab", "asa", "oyj", "oy", "as", "bv",
    "gmbh", "kgaa", "sas", "srl", "aps", "aktiengesellschaft", "trust",
))

# Abbreviations a source sheet uses where the canonical row spells it out.
_TOKEN_ALIASES = {
    "intl": "international",
    "natl": "national",
    "assoc": "association",
    "svcs": "services",
    "svc": "service",
    "tech": "technologies",
    "technology": "technologies",
    "mfg": "manufacturing",
    "pharm": "pharmaceuticals",
    "pharma": "pharmaceuticals",
    "pharmaceutical": "pharmaceuticals",
    "labs": "laboratories",
    "lab": "laboratories",
    "laboratory": "laboratories",
    "bancorp": "bank",
    "banks": "bank",
    "resource": "resources",
    "industry": "industries",
    "system": "systems",
    "solution": "solutions",
    "brand": "brands",
    "store": "stores",
}

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")

# A share-class qualifier and everything after it. Canonical rows sometimes
# carry the listing description rather than the company name — "Paramount
# Skydance Corporation Class B Common Stock" — and the class letter would
# otherwise survive tokenising as a stray single-character token that nothing
# on the source side can match. Dropping single letters wholesale would be
# worse: "A O Smith" needs its initials.
_SHARE_CLASS = re.compile(r"\bclass\s+[a-z]\b.*$", re.I)


def _significant_tokens(name: Optional[str]) -> frozenset:
    """
    The tokens of a company name that actually identify it.

    Everything after a "/" is dropped first: source sheets carry Bloomberg-style
    qualifiers there — "CDW Corp/DE", "Xylem Inc/NY", "Cooper Cos Inc/The" — that
    name a state of incorporation or an article, never the company.

    Accents are folded, punctuation split on, abbreviations expanded, and legal
    or filler tokens removed. What is left has to match exactly, so this is a
    normaliser, not a fuzzy matcher.
    """
    text = _SHARE_CLASS.sub(" ", (name or "").split("/")[0])
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    # "&" reads as "and" in one sheet and is omitted in the other; either way it
    # is noise once "and" is dropped.
    folded = folded.replace("&", " and ")
    tokens = [t for t in _TOKEN_SPLIT.split(folded) if t]

    # Rejoin runs of single characters. Splitting on punctuation turns "p.l.c."
    # into three tokens that no noise list can catch, so "British American
    # Tobacco" fails against "British American Tobacco p.l.c." — and it turns
    # "A. O. Smith" into two, which must still equal the "A O Smith" spelling.
    # Collapsing the run handles both: "plc" becomes noise, "ao" survives on
    # each side identically.
    merged: List[str] = []
    run: List[str] = []
    for token in tokens + [None]:
        if token is not None and len(token) == 1:
            run.append(token)
            continue
        if run:
            merged.append("".join(run))
            run = []
        if token is not None:
            merged.append(token)

    expanded = [_TOKEN_ALIASES.get(t, t) for t in merged]
    return frozenset(t for t in expanded if t not in _NOISE_TOKENS)


def _name_core(name: Optional[str]) -> str:
    """
    A company name reduced to the part that identifies it.

    Normalised, a leading or trailing "the" removed, then legal suffixes
    stripped from the end until none remain. "3M Co" and "3M Company" both
    reduce to "3m"; "AES Corp/The" and "The AES Corporation" both to "aes".

    This is for COMPARING a source name against a matched one. It is not a
    replacement for normalise_company_name(), which keeps suffixes on purpose so
    grouping never merges a parent with its subsidiary.
    """
    core = normalise_company_name(name)
    if core.startswith("the"):
        core = core[3:]
    if core.endswith("the"):
        core = core[:-3]
    changed = True
    while changed and core:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if core.endswith(suffix) and len(core) > len(suffix):
                core = core[:-len(suffix)]
                changed = True
                break
    return core


def _name_matches(requested: str, matched: Optional[str]) -> bool:
    """
    Whether a resolved company is plausibly the company that was asked for.

    Without this a TICKER match is accepted on the ticker alone, and tickers are
    not globally unique: "AAL" is Anglo American on the LSE and American
    Airlines on NASDAQ, "III" is 3i Group on the LSE and Information Services
    Group on NASDAQ. Both resolved to the American company, both held documents,
    and both would have been extracted and stored under the British company's
    name. A wrong company that scores is far worse than one that does not
    resolve, because nothing downstream can tell that it is wrong.
    """
    if not matched:
        return False
    if normalise_company_name(requested) == normalise_company_name(matched):
        return True

    left, right = _significant_tokens(requested), _significant_tokens(matched)
    if not left or not right:
        # A name that is all legal boilerplate tells us nothing; fall back to
        # comparing the suffix-stripped cores.
        return bool(_name_core(requested)) and _name_core(requested) == _name_core(matched)

    # EQUALITY, not subset. Subset would accept "Barclays" for "Barclays Bank
    # UK PLC" — {barclays} is a subset of {barclays, bank, uk} — and attribute a
    # subsidiary's filings to its parent. Every identifying token has to be
    # present on both sides.
    if left == right:
        return True

    # One remaining allowance: a single trailing-character difference on an
    # otherwise identical single token, for plurals ("equitie"/"equities").
    if len(left) == 1 and len(right) == 1:
        one, two = next(iter(left)), next(iter(right))
        shorter, longer = sorted((one, two), key=len)
        return (len(shorter) >= 6 and longer.startswith(shorter)
                and len(longer) - len(shorter) <= 2)
    return False


def _documents_for(company_id: int, uid: str, years: int) -> Dict[str, Any]:
    """
    What the progressive engine would actually read for this company.

    Uses the same grouped fetch and the same selection the task uses, so a
    company reported as ready here is one the run can genuinely work on.
    """
    from data_ingestion_engine.utils.ren.company_group import (
        fetch_reports_for_company_group)

    payload = fetch_reports_for_company_group(company_id, uid)
    docs = select_all_ren_documents(payload, years=years)
    by_year: Dict[int, set] = {}
    for doc in docs:
        by_year.setdefault(doc["year"], set()).add(doc["document_type"])
    return {"documents": len(docs),
            "company_years": len(by_year),
            "years": sorted(by_year, reverse=True),
            "types": sorted({t for ts in by_year.values() for t in ts})}


def load_overrides(path: Optional[str]) -> Dict[str, int]:
    """
    Hand-assigned company ids, keyed by ticker and by normalised company name.

    Automatic resolution cannot reach everything. A ticker that collides with a
    US instrument (BAB is Babcock International in London and an Invesco ETF in
    New York), a company stored under a name no search reaches, a rename like
    General Electric to GE Aerospace — each needs a person to decide, once.

    An override BYPASSES the name check, because that check exists to catch
    machine guesses and this is not one. The id is still verified to exist and
    to hold documents, so a typo shows up as no_documents rather than silently
    extracting the wrong company.

    CSV with columns: company, ticker, company_id. Rows with a blank
    company_id are ignored, so the file can carry the whole worklist.
    """
    overrides: Dict[str, int] = {}
    if not path:
        return overrides
    if not os.path.exists(path):
        logger.warning("Override file not found, ignoring: %s", path)
        return overrides

    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in _csv.DictReader(handle):
            row = {str(k).strip().lower(): (v or "").strip()
                   for k, v in row.items()}
            raw = row.get("company_id", "")
            if not raw:
                continue
            try:
                company_id = int(float(raw))
            except ValueError:
                logger.warning("Override for %r has a non-numeric company_id "
                               "%r — skipped", row.get("company"), raw)
                continue
            if row.get("ticker"):
                overrides[f"ticker:{row['ticker'].upper()}"] = company_id
            if row.get("company"):
                overrides[f"name:{normalise_company_name(row['company'])}"] = company_id

    logger.info("%s override(s) loaded from %s",
                len({v for k, v in overrides.items()}), path)
    return overrides


def resolve_one(entry: Dict[str, Any], uid: str, years: int,
                overrides: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Try each strategy until one lands on a company holding documents."""
    name = (entry.get("company") or "").strip()
    ticker = (entry.get("ticker") or "").strip()

    # A hand-assigned id wins outright — that is the point of it.
    override_id = (overrides or {}).get(f"ticker:{ticker.upper()}")
    if override_id is None:
        override_id = (overrides or {}).get(
            f"name:{normalise_company_name(name)}")
    if override_id is not None:
        record = fetch_company(override_id, uid) or {}
        matched_name = record.get("name") or record.get("company_name")
        try:
            found = _documents_for(override_id, uid, years)
        except Exception as e:
            found = {"documents": 0, "company_years": 0, "years": [], "types": []}
            logger.warning("  override %s for %r: document check failed — %s",
                           override_id, name, e)
        status = "ready" if found["documents"] else "no_documents"
        return {**entry, "company_id": override_id, "strategy": "override",
                "matched_name": matched_name, "status": status, **found,
                "detail": ("hand-assigned" if found["documents"] else
                           "hand-assigned but holds no readable documents — "
                           "check the id")}

    attempts = [
        ("name+ticker", {"Company Name": name, "Ticker Symbol": ticker}),
        ("name", {"Company Name": name}),
    ]

    first_match: Optional[int] = None
    first_strategy: Optional[str] = None
    rejected: List[Dict[str, Any]] = []

    for strategy, metadata in attempts:
        if not metadata.get("Company Name"):
            continue
        try:
            company_id = resolve_company_id(metadata, uid)
        except Exception as e:
            logger.debug("  %s: %s failed — %s", name, strategy, e)
            continue
        if not company_id:
            continue

        record = fetch_company(company_id, uid) or {}
        matched_name = record.get("name") or record.get("company_name")

        # Verify BEFORE the document check. A ticker collision resolves to a
        # real company holding real documents — the check that catches it is the
        # name, not the document count.
        if not _name_matches(name, matched_name):
            rejected.append({"company_id": company_id, "strategy": strategy,
                             "matched_name": matched_name})
            logger.info("  %s (%s): rejected [%s] %r — different company",
                        name, ticker, company_id, matched_name)
            continue

        if first_match is None:
            first_match, first_strategy = company_id, strategy
        try:
            found = _documents_for(company_id, uid, years)
        except Exception as e:
            logger.debug("  %s: document check failed — %s", name, e)
            continue
        if found["documents"]:
            return {**entry, "company_id": company_id, "strategy": strategy,
                    "matched_name": matched_name,
                    "status": "ready", **found}

    # Last resort: the search endpoint. find_companies_by_name() accepts only an
    # exact normalised match, which misses the very common case where the source
    # list drops the legal suffix the canonical table carries — "Antofagasta"
    # against "Antofagasta plc". _suffix_variants() covers that, and ONLY that:
    # the remainder has to be a bare legal form, so "Barclays" still cannot
    # match "Barclays Bank UK PLC" (remainder "bankukplc" is not a suffix).
    for label, candidate_name in _suffix_variants(name):
        try:
            for candidate in find_companies_by_name(candidate_name, uid):
                company_id = candidate["company_id"]
                if not _name_matches(name, candidate["name"]):
                    rejected.append({"company_id": company_id,
                                     "strategy": label,
                                     "matched_name": candidate["name"]})
                    continue
                if first_match is None:
                    first_match, first_strategy = company_id, label
                found = _documents_for(company_id, uid, years)
                if found["documents"]:
                    return {**entry, "company_id": company_id,
                            "strategy": label,
                            "matched_name": candidate["name"],
                            "status": "ready", **found}
        except Exception as e:
            logger.debug("  %s: search %r failed — %s", name, candidate_name, e)

    if first_match is not None:
        record = fetch_company(first_match, uid) or {}
        return {**entry, "company_id": first_match, "strategy": first_strategy,
                "matched_name": record.get("name") or record.get("company_name"),
                "documents": 0, "company_years": 0, "years": [], "types": [],
                "status": "no_documents",
                "rejected": rejected,
                "detail": "matched a canonical company that holds nothing the "
                          "engine can read — probably the wrong twin"}

    return {**entry, "company_id": None, "strategy": None, "matched_name": None,
            "documents": 0, "company_years": 0, "years": [], "types": [],
            "status": "unresolved",
            "rejected": rejected,
            "detail": (
                f"rejected {len(rejected)} ticker/name match(es) as a different "
                f"company: "
                + "; ".join(f"[{r['company_id']}] {r['matched_name']!r}"
                            for r in rejected)) if rejected else
                "no canonical company matched this name or ticker"}



def _suggest(results: List[Dict[str, Any]], uid: str, years: int,
             out_csv: str) -> None:
    """
    For every row that did not resolve, show what the database DOES hold.

    Automatic matching refuses anything it cannot verify, which is right, but it
    leaves no trail to the correct answer. This searches by progressively
    shorter fragments of the name and prints each candidate with its document
    count, so the id can be chosen rather than guessed — then writes an override
    skeleton with those rows pre-filled and company_id blank.
    """
    stuck = [r for r in results if r["status"] != "ready"]
    if not stuck:
        print("\n  Nothing to suggest — every row resolved.")
        return

    print(f"\n=== candidates for {len(stuck)} unresolved row(s) ===")
    print("  (document counts are what the progressive engine would read)\n")

    for record in stuck:
        name = record["company"]
        print(f"  {name!r} ({record['ticker']})")
        for x in record.get("rejected") or []:
            print(f"      rejected already: [{x['company_id']}] {x['matched_name']!r}")

        # Longest fragment first: the full name, then dropping trailing words.
        tokens = [t for t in name.replace("/", " ").split() if t]
        seen_ids = set()
        shown = 0
        for cut in range(len(tokens), 0, -1):
            if shown >= 6:
                break
            fragment = " ".join(tokens[:cut])
            if len(fragment) < 3:
                continue
            try:
                candidates = find_companies_by_name(fragment, uid)
            except Exception:
                continue
            for candidate in candidates:
                if candidate["company_id"] in seen_ids or shown >= 6:
                    continue
                seen_ids.add(candidate["company_id"])
                try:
                    found = _documents_for(candidate["company_id"], uid, years)
                except Exception:
                    found = {"documents": 0, "company_years": 0}
                print(f"      [{candidate['company_id']:>7}] "
                      f"{candidate['name']!r}  "
                      f"{found['documents']} doc(s), "
                      f"{found['company_years']} company-year(s)")
                shown += 1
        if not shown:
            print("      nothing found by name — needs an id from the database")
        print()

    path = os.path.splitext(out_csv)[0] + "_overrides.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = _csv.writer(handle)
        writer.writerow(["company", "ticker", "company_id", "note"])
        for record in stuck:
            writer.writerow([record["company"], record["ticker"], "",
                             record.get("detail") or record["status"]])
    print(f"  Wrote {path} — fill in company_id and pass it as --overrides")



def _collapse_duplicates(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Mark rows that resolved to a company another row already covers.

    A source list can name one company twice — dual share classes are the usual
    reason: Alphabet as GOOG and GOOGL, Fox as FOX and FOXA, News Corp as NWS
    and NWSA. Each class resolves to its own canonical id, but both ids sit in
    the same normalised-name group, so fetch_reports_for_company_group() hands
    them the SAME documents. Running both spends every model call twice and
    produces two leaderboard rows for one company.

    The row with the most documents wins, lowest id breaking the tie — the lower
    id is usually the original canonical row rather than a later duplicate. The
    loser is marked `duplicate` rather than deleted, so the report still shows
    every source row and why it did not run.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for record in results:
        if record["status"] != "ready":
            continue
        key = normalise_company_name(record.get("matched_name")
                                     or record.get("company"))
        incumbent = best.get(key)
        if incumbent is None or (
                (record.get("documents") or 0),
                -(record.get("company_id") or 0),
        ) > (
                (incumbent.get("documents") or 0),
                -(incumbent.get("company_id") or 0),
        ):
            best[key] = record

    kept = {id(r) for r in best.values()}
    for record in results:
        if record["status"] != "ready" or id(record) in kept:
            continue
        key = normalise_company_name(record.get("matched_name")
                                     or record.get("company"))
        winner = best[key]
        record["status"] = "duplicate"
        record["detail"] = (
            f"same company as row {winner['row']} "
            f"({winner['ticker']} -> id {winner['company_id']}); both ids read "
            f"the same documents, so running both would double the cost")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        parents=[_PRE],
        description="Resolve a name/ticker list to canonical company ids.")
    parser.add_argument("--uid", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--name-column", default="Company")
    parser.add_argument("--ticker-column", default="Ticker")
    parser.add_argument("--years", type=int, default=2,
                        help="how many recent years to check for documents")
    parser.add_argument("--out", default="resolved_cohort.json")
    parser.add_argument("--out-csv", default="resolved_cohort.csv")
    parser.add_argument("--limit", type=int, help="resolve only the first N rows")
    parser.add_argument("--overrides",
                        help="CSV of hand-assigned ids: company,ticker,company_id")
    parser.add_argument("--suggest", action="store_true",
                        help="for every row that does NOT resolve, search DataX "
                             "by name and print candidate ids with their "
                             "document counts, then write an override skeleton")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not os.path.exists(args.csv):
        logger.error("Not found: %s", args.csv)
        return 2

    with open(args.csv, newline="", encoding="utf-8-sig") as handle:
        rows = [{str(k).strip(): v for k, v in r.items()}
                for r in _csv.DictReader(handle)]

    entries = []
    for index, row in enumerate(rows, start=2):
        name = (row.get(args.name_column) or "").strip()
        if not name:
            continue
        entries.append({"row": index, "company": name,
                        "ticker": (row.get(args.ticker_column) or "").strip()})
    if args.limit:
        entries = entries[:args.limit]

    logger.info("%s company/companies to resolve (%s workers)",
                len(entries), WORKERS)

    overrides = load_overrides(args.overrides)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(
            lambda e: resolve_one(e, args.uid, args.years, overrides), entries))
    results.sort(key=lambda r: r["row"])

    results = _collapse_duplicates(results)
    counts = Counter(r["status"] for r in results)
    ready = [r for r in results if r["status"] == "ready"]

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({
            "meta": {"source": args.csv, "years": args.years,
                     "total": len(results), **dict(counts)},
            "company_ids": [r["company_id"] for r in ready],
            "results": results,
        }, handle, indent=2, default=str)

    with open(args.out_csv, "w", newline="", encoding="utf-8") as handle:
        writer = _csv.DictWriter(handle, fieldnames=REPORT_FIELDS,
                                 extrasaction="ignore")
        writer.writeheader()
        for record in results:
            writer.writerow({**record, "years": ", ".join(
                str(y) for y in record.get("years") or [])})

    print(f"\n  ready          {counts.get('ready', 0)}")
    if counts.get("duplicate"):
        print(f"  duplicate      {counts['duplicate']} "
              f"(same company reached by another row)")
    print(f"  no documents   {counts.get('no_documents', 0)}")
    print(f"  unresolved     {counts.get('unresolved', 0)}")
    print(f"  ------------------------")
    print(f"  total          {len(results)}")

    if ready:
        print(f"\n  matched by strategy: "
              f"{dict(Counter(r['strategy'] for r in ready))}")
        print(f"  company-years to run: {sum(r['company_years'] for r in ready)}")
        print(f"  documents to read:    {sum(r['documents'] for r in ready)}")

    # A name that resolved to a DIFFERENT name is the thing most worth a human
    # eye — that is where a wrong match hides.
    suspicious = [r for r in ready
                  if r.get("matched_name")
                  and normalise_company_name(r["matched_name"])
                  != normalise_company_name(r["company"])]
    if suspicious:
        print(f"\n  {len(suspicious)} matched a DIFFERENT name — review these:")
        for record in suspicious[:20]:
            print(f"    {record['company']!r} ({record['ticker']}) "
                  f"-> [{record['company_id']}] {record['matched_name']!r}")
        if len(suspicious) > 20:
            print(f"    ... and {len(suspicious) - 20} more in {args.out_csv}")

    if args.suggest:
        _suggest(results, args.uid, args.years, args.out_csv)

    print(f"\n  Wrote {args.out} and {args.out_csv}")
    print(f"  Review before running. The `company_ids` array in {args.out} is "
          f"what the endpoint takes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
