#!/usr/bin/env python
"""
Repair the `year` on company-documents that were ingested with the wrong year.

The Ingested-19_08_2026 mastersheet was ingested before its Year column was
correct, so every document it produced landed in DataX as year=1900. The sheet
itself now carries the right year for all 161 rows, which makes it the source of
truth: this script walks the sheet, finds the document each row created, and
PATCHes `year` to the sheet's value.

Matching is filename-first. process_download() builds the GCS key from
`{company}_{title}_{report_type}_{year}.pdf`, so a document's stored
`document_url` still carries the company/title/report-type stem the row was
ingested under. Stripping the trailing `_<year>` gives a key that survives the
very mistake we are fixing. Title+report_type and title-only are the fallbacks,
in that order; anything that matches two documents is reported, never guessed.

Only documents whose stored year is in --wrong-year (default: 1900) are touched.
A row whose document already holds the sheet's year is left alone and reported
as `already-correct`, so re-running is safe.

DRY RUN BY DEFAULT — nothing is written without --apply.

Usage:
    # 1. see what would change (no writes)
    python -m data_ingestion_engine.scripts.fix_document_years \
        --uid <uid> \
        --csv "data_ingestion_engine/Mastersheet- Ingestion V - Ingested-19_08_2026.csv" \
        --api-url https://<prod-datax-host> \
        --out year_fix_report.csv

    # 2. apply it
    python -m data_ingestion_engine.scripts.fix_document_years \
        --uid <uid> \
        --csv "data_ingestion_engine/Mastersheet- Ingestion V - Ingested-19_08_2026.csv" \
        --api-url https://<prod-datax-host> \
        --out year_fix_report.csv --apply

--api-url overrides DATAX_API_URL for this process only; without it the script
targets whatever .env points at (currently the dev backend).
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

# --api-url has to win before store_data is imported: it reads DATAX_API_URL at
# module scope and raises if it is unset. Parsed here, used again in main().
_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument("--api-url")
_pre_args, _ = _PRE.parse_known_args()
if _pre_args.api_url:
    os.environ["DATAX_API_URL"] = _pre_args.api_url.rstrip("/")

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_ingestion_engine.settings")
django.setup()

import pandas as pd  # noqa: E402

from data_ingestion_engine.utils.coerce_data import _coerce_year  # noqa: E402
from data_ingestion_engine.utils.handle_metadata_value import (  # noqa: E402
    handle_metadata_value)
from data_ingestion_engine.utils.process_download import (  # noqa: E402
    parse_ticker_symbols)
from data_ingestion_engine.utils.sanitizeFilename import (  # noqa: E402
    sanitize_filename)
from data_ingestion_engine.utils.store_data import (  # noqa: E402
    DATAX_API_URL, fetch_reports_for_company, get_company_id, patch_report)

logger = logging.getLogger("fix_document_years")

DEFAULT_CSV = ("data_ingestion_engine/"
               "Mastersheet- Ingestion V - Ingested-19_08_2026.csv")

# Report columns, in the order they are written.
REPORT_FIELDS = [
    "row", "company_name", "isin", "company_id", "report_id",
    "sheet_year", "stored_year", "match_tier", "status", "detail",
    "document_title", "document_url", "link",
]


# ---------------------------------------------------------------------------
# Row -> the filename stem process_download() would have produced
# ---------------------------------------------------------------------------

def row_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    """Whitespace-normalised sheet row, mirroring what ingestion consumed."""
    return {str(k).strip(): v for k, v in row.items()}


def report_type_for_filename(meta: Dict[str, Any]) -> str:
    """
    The report-type token as it went into the filename.

    process_download() rewrites 'Earnings Call' to 'earnings_call_transcript'
    *before* building the name, so the stem has to do the same or every earnings
    transcript misses.
    """
    value = handle_metadata_value(meta.get("Type of Report"))
    return "earnings_call_transcript" if value == "Earnings Call" else value


def filename_stem(meta: Dict[str, Any]) -> str:
    """
    The `{company}_{title}_{report_type}_` prefix of the ingested GCS key.

    Deliberately excludes the year: the year in the stored key is the wrong one
    we are here to correct, so keying on it would match nothing.

    Returns "" when sanitize_filename would have truncated and hashed the name —
    the hash is taken over the full string *including* the year, so the stem is
    not reconstructable and the caller must fall back to title matching.
    """
    company_name = handle_metadata_value(meta.get("Company Name"))
    title = handle_metadata_value(meta.get("Title of the Report"))
    report_type = report_type_for_filename(meta)
    year = _coerce_year(meta.get("Year"), default=1900)

    full = (f"{company_name}_{title}_{report_type}_{year}.pdf"
            .replace(" ", "_")
            .replace("/", "|"))
    sanitized = sanitize_filename(full)
    if sanitized.endswith(f"_{year}.pdf"):
        return normalise(sanitized[: -len(f"{year}.pdf")])
    # Truncated + hashed: unreconstructable without the ingested year.
    return ""


def normalise(text: Optional[str]) -> str:
    """Casefold and collapse separator runs so `_`, `-` and spaces compare equal."""
    if not text:
        return ""
    return re.sub(r"[\s_\-]+", "_", str(text).strip().lower())


def stored_stem(document_url: Optional[str]) -> str:
    """The `{company}_{title}_{report_type}_` prefix of a stored document URL."""
    if not document_url:
        return ""
    basename = unquote(urlparse(str(document_url)).path).rsplit("/", 1)[-1]
    if basename.lower().endswith(".pdf"):
        basename = basename[:-4]
    # Drop the trailing `_<4-digit year>` the ingester appended.
    basename = re.sub(r"_\d{4}$", "_", basename)
    return normalise(basename)


# ---------------------------------------------------------------------------
# Company resolution
# ---------------------------------------------------------------------------

def resolve_company(meta: Dict[str, Any], uid: str) -> Optional[int]:
    """
    Look up (never create) the company a row belongs to.

    ISIN alone when the row has one: duplicate company rows share an ISIN, and
    feeding the extra identifiers steers matching onto the empty twin that holds
    no documents. Rows without an ISIN fall back to the full identifier set,
    which is all they have.
    """
    isin = handle_metadata_value(meta.get("ISIN")).strip()
    if isin and isin.upper() != "N/A":
        company_id = get_company_id(
            metadata={"Company Name": meta.get("Company Name", ""), "ISIN": isin},
            uid=uid,
            create=False,
        )
        if company_id:
            return company_id

    tickers = parse_ticker_symbols(meta.get("Ticker Symbol"))
    return get_company_id(
        metadata=meta,
        uid=uid,
        ticker=tickers[0] if tickers else "",
        exchange=handle_metadata_value(meta.get("Exchange")),
        tickers=tickers,
        create=False,
    )


def company_documents(company_id: int, uid: str) -> List[Dict[str, Any]]:
    """Every document on a company, reports and filings merged."""
    payload = fetch_reports_for_company(company_id, uid) or {}
    if isinstance(payload, list):
        return list(payload)
    docs = list(payload.get("reports") or []) + list(payload.get("filings") or [])
    return docs


def document_id(doc: Dict[str, Any]) -> Optional[int]:
    """The id patch_report() addresses; the shape varies by endpoint version."""
    for key in ("report_id", "id", "document_id"):
        value = doc.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def document_year(doc: Dict[str, Any]) -> Optional[int]:
    try:
        return int(doc.get("year"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_documents(
    meta: Dict[str, Any],
    docs: List[Dict[str, Any]],
    fixable_year,
    target_year: int,
) -> Tuple[List[Dict[str, Any]], str, str]:
    """
    Find the document(s) a sheet row could have produced.

    Tried in descending confidence: filename stem, title+report_type, title
    alone; the first tier that matches anything wins. Returns
    (candidates, tier, detail). More than one candidate is left for the caller
    to resolve — the sheet legitimately holds several rows that sanitize to one
    filename (two Associated British Foods annual reports, for instance), and
    those are separable by year rather than by name.

    Year is what breaks the ties, because names alone cannot. Stripping the
    year off the stem is what lets a row match the document it mis-ingested,
    but it also makes that row match the company's *earlier* filings —
    "ageas_SA_NV_Annual_Report_Annual_Report_" is the 2024 annual report's stem
    just as much as the 2025 one's. So a tier's matches are narrowed to the
    documents actually eligible for repair, and failing that to one already
    holding the sheet's year; the untouched prior-year filing is never a
    candidate when a repairable document exists.
    """
    if not docs:
        return [], "", "company has no documents"

    def narrow(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        repairable = [d for d in candidates if fixable_year(document_year(d))]
        if repairable:
            return repairable
        already = [d for d in candidates if document_year(d) == target_year]
        return already or candidates

    tiers: List[Tuple[str, List[Dict[str, Any]]]] = []

    stem = filename_stem(meta)
    if stem:
        tiers.append(("filename", [d for d in docs
                                   if stored_stem(d.get("document_url")) == stem]))

    title = normalise(meta.get("Title of the Report"))
    report_type = normalise(report_type_for_filename(meta))
    if title:
        tiers.append(("title+type", [
            d for d in docs
            if normalise(d.get("document_title")) == title
            and normalise(d.get("report_type")) == report_type
        ]))
        tiers.append(("title", [d for d in docs
                                if normalise(d.get("document_title")) == title]))

    for tier, candidates in tiers:
        if candidates:
            return narrow(candidates), tier, ""

    return [], "", "no document matched"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fix wrong years on ingested company-documents from the "
                    "mastersheet that produced them.")
    p.add_argument("--uid", default=os.getenv("DATAX_UID"),
                   help="User id minted as the internal JWT's `sub` — the user "
                        "the repair is attributed to. Defaults to $DATAX_UID.")
    p.add_argument("--csv", default=DEFAULT_CSV,
                   help=f"Ingested mastersheet (default: {DEFAULT_CSV})")
    p.add_argument("--api-url",
                   help="Override DATAX_API_URL for this run, e.g. the prod "
                        "backend. Without it, .env decides.")
    p.add_argument("--wrong-year", type=int, action="append", default=None,
                   help="Stored year considered wrong; repeatable. "
                        "Default: 1900.")
    p.add_argument("--any-wrong-year", action="store_true",
                   help="Fix every document whose stored year differs from the "
                        "sheet, not just --wrong-year ones. DANGEROUS: a "
                        "company's correctly-dated earlier filing shares its "
                        "stem with this year's row, so this can relabel a good "
                        "2024 annual report as 2025. Read the dry-run report "
                        "row by row before using it.")
    p.add_argument("--out", default="year_fix_report.csv",
                   help="Per-row outcome report (default: year_fix_report.csv)")
    p.add_argument("--apply", action="store_true",
                   help="Actually PATCH. Without it the run is a dry run.")
    args = p.parse_args()
    if not args.uid:
        p.error("--uid is required (or set DATAX_UID in .env)")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    wrong_years = set(args.wrong_year or [1900])

    def is_fixable(stored: Optional[int], target: int) -> bool:
        """Whether a stored year is one this run is allowed to overwrite."""
        if stored is None or stored == target:
            return False
        return args.any_wrong_year or stored in wrong_years

    mode = "APPLY (writes to DataX)" if args.apply else "DRY RUN (no writes)"
    logger.info("Target : %s", DATAX_API_URL)
    logger.info("Mode   : %s", mode)
    logger.info("Sheet  : %s", args.csv)
    logger.info("Fixing : %s", "any year that differs from the sheet"
                if args.any_wrong_year else
                f"stored year in {sorted(wrong_years)}")
    logger.info("")

    df = pd.read_csv(args.csv).dropna(how="all")
    rows = df.to_dict(orient="records")
    logger.info("Read %s rows.", len(rows))

    # Resolve companies first, then read each company's documents once —
    # the sheet has several rows per company and the list endpoint is heavy.
    resolved: List[Tuple[int, Dict[str, Any], Optional[int], str]] = []
    by_company: Dict[int, List[int]] = defaultdict(list)
    # (company_id, stem) -> the sheet years claiming that filename. Several rows
    # sanitizing to one name is normal; when they all want the same year the
    # collision is harmless, and this is what says so.
    stem_years: Dict[Tuple[int, str], set] = defaultdict(set)

    for index, raw in enumerate(rows, start=2):  # +2: 1-based, past the header
        meta = row_metadata(raw)
        company_id = resolve_company(meta, args.uid)
        stem = filename_stem(meta)
        resolved.append((index, meta, company_id, stem))
        if company_id:
            by_company[company_id].append(index)
            if stem:
                stem_years[(company_id, stem)].add(
                    _coerce_year(meta.get("Year"), default=1900))

    unresolved = sum(1 for _, _, cid, _ in resolved if not cid)
    logger.info("Resolved %s companies for %s rows (%s rows unresolved).",
                len(by_company), len(rows) - unresolved, unresolved)

    doc_cache: Dict[int, List[Dict[str, Any]]] = {}
    for company_id in by_company:
        doc_cache[company_id] = company_documents(company_id, args.uid)

    # A document must not be claimed by two sheet rows — the sheet has a
    # duplicated Link, and near-identical titles are common within a company.
    claimed: Dict[int, int] = {}
    results: List[Dict[str, Any]] = []
    counts: Dict[str, int] = defaultdict(int)

    for index, meta, company_id, stem in resolved:
        record = {
            "row": index,
            "company_name": handle_metadata_value(meta.get("Company Name")),
            "isin": handle_metadata_value(meta.get("ISIN")),
            "company_id": company_id or "",
            "report_id": "",
            "sheet_year": _coerce_year(meta.get("Year"), default=1900),
            "stored_year": "",
            "match_tier": "",
            "status": "",
            "detail": "",
            "document_title": "",
            "document_url": "",
            "link": handle_metadata_value(meta.get("Link")),
        }

        if not company_id:
            record["status"] = "company-unresolved"
            record["detail"] = "no canonical company matched this row"
            results.append(record)
            counts[record["status"]] += 1
            continue

        candidates, tier, detail = match_documents(
            meta, doc_cache.get(company_id, []),
            fixable_year=lambda stored: is_fixable(stored, record["sheet_year"]),
            target_year=record["sheet_year"])
        record["match_tier"] = tier

        if not candidates:
            record["status"] = "unmatched"
            record["detail"] = detail
            results.append(record)
            counts[record["status"]] += 1
            continue

        if len(candidates) == 1:
            doc = candidates[0]
        else:
            # Several documents share this row's name. That is only safe to
            # resolve when every sheet row competing for the name asks for the
            # same year — then it does not matter which row takes which
            # document, they all end up correct. Rows that disagree (the same
            # filename wanted at 2025 and 2026) are a curation problem: those
            # two uploads also collided on one GCS key, so a human has to say
            # which document is which.
            peers = stem_years.get((company_id, stem), set())
            ids = ", ".join(str(document_id(d)) for d in candidates)
            if len(peers) != 1:
                record["status"] = "ambiguous"
                record["detail"] = (
                    f"{len(candidates)} documents match ({ids}) and the sheet "
                    f"rows sharing this filename disagree on the year "
                    f"({sorted(peers)})")
                results.append(record)
                counts[record["status"]] += 1
                continue

            doc = next((d for d in candidates
                        if document_id(d) not in claimed), None)
            if doc is None:
                record["status"] = "ambiguous"
                record["detail"] = (
                    f"more sheet rows than documents for this filename "
                    f"({ids} all claimed)")
                results.append(record)
                counts[record["status"]] += 1
                continue
            record["detail"] = (f"one of {len(candidates)} same-name documents "
                                f"({ids}); all rows agree on {sorted(peers)[0]}")

        report_id = document_id(doc)
        stored = document_year(doc)
        record.update({
            "report_id": report_id if report_id is not None else "",
            "stored_year": stored if stored is not None else "",
            "document_title": handle_metadata_value(doc.get("document_title")),
            "document_url": handle_metadata_value(doc.get("document_url")),
        })

        if report_id is None:
            record["status"] = "unmatched"
            record["detail"] = "matched document carries no id to patch"
            results.append(record)
            counts[record["status"]] += 1
            continue

        if report_id in claimed:
            record["status"] = "ambiguous"
            record["detail"] = f"document already claimed by row {claimed[report_id]}"
            results.append(record)
            counts[record["status"]] += 1
            continue
        claimed[report_id] = index

        target = record["sheet_year"]
        if stored == target:
            record["status"] = "already-correct"
            results.append(record)
            counts[record["status"]] += 1
            continue

        if not is_fixable(stored, target):
            record["status"] = "skipped"
            record["detail"] = (f"stored year {stored} is not in "
                                f"{sorted(wrong_years)}; use --any-wrong-year "
                                f"to include it")
            results.append(record)
            counts[record["status"]] += 1
            continue

        if not args.apply:
            record["status"] = "would-fix"
            results.append(record)
            counts[record["status"]] += 1
            continue

        if patch_report(report_id, target, "year", args.uid):
            record["status"] = "fixed"
        else:
            record["status"] = "patch-failed"
            record["detail"] = "PATCH did not return 200 — see log above"
        results.append(record)
        counts[record["status"]] += 1

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    logger.info("")
    for status in sorted(counts):
        logger.info("%-20s %s", status, counts[status])
    logger.info("%-20s %s", "total", len(results))
    logger.info("")
    logger.info("Report written to %s", args.out)
    if not args.apply:
        logger.info("Dry run — nothing was written. Re-run with --apply.")

    # Non-zero when a row needs a human: unresolved, unmatched, ambiguous, or a
    # failed PATCH. already-correct / skipped / would-fix / fixed are all fine.
    needs_attention = sum(counts[s] for s in
                          ("company-unresolved", "unmatched", "ambiguous",
                           "patch-failed"))
    return 1 if needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
