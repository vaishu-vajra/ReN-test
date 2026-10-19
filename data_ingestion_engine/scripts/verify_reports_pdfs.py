#!/usr/bin/env python3
import os
import sys
import csv
import json
import argparse
from io import BytesIO
from typing import Dict, Any, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- Third-party ----
from pypdf import PdfReader

# ---- Project imports ----
from data_ingestion_engine.utils.download_file import download_file
from data_ingestion_engine.utils.gc_service import download_blob_bytes_from_url
from data_ingestion_engine.utils.store_data import fetch_reports_for_company
from data_ingestion_engine.utils.store_data import fetch_all_reports


# -------------------------
# CLI
# -------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Download+verify PDFs from internal API and output failing items in ORIGINAL DB/API shape."
    )
    p.add_argument("--uid", required=True,
                   help="Auth UID/token passed into your internal fetch functions")

    # Fetch modes
    p.add_argument("--mode", choices=["all-docs", "by-company"], default="all-docs",
                   help="Use `all-docs` to call fetch_all_reports(uid). Use `by-company` to loop companies via fetch_reports_for_company().")

    p.add_argument("--company-ids-file",
                   help="Path to a text file with one company_id per line (for by-company mode).")
    p.add_argument("--year", type=int, default=None,
                   help="Optional year filter passed to fetch_reports_for_company in by-company mode.")

    # Payload shape knobs (to match your API/DB item keys)
    p.add_argument("--items-field", default="items",
                   help="If fetch function returns a dict, this is the list key. If it returns a list, this is ignored.")
    p.add_argument("--id-field", default="id",
                   help="Report id field (used only for logs/CSV).")
    p.add_argument("--url-field", default="url",
                   help="Field name that contains the storage URL in each item.")

    # Concurrency + output
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--out-json", default="bad_reports.json",
                   help="Output JSON containing ONLY failing items in ORIGINAL DB/API shape.")
    p.add_argument("--out-csv", default="bad_reports.csv",
                   help="CSV summary (id, url, status, reason) for convenience.")
    p.add_argument("-q", "--quiet", action="store_true")

    return p.parse_args()


# -------------------------
# Fetching (ALL DOCS)
# -------------------------
def iter_all_reports(uid: str,
                     items_field: str) -> List[Dict[str, Any]]:
    """
    Return ALL reports using fetch_all_reports(uid), with NO reshaping.
    Output is a list of dicts EXACTLY as returned by your API/DB.
    Accepts either:
      - payload: List[dict]
      - payload: Dict with a list under `items_field`
    """
    try:
        payload = fetch_all_reports(uid=uid)
    except TypeError:
        payload = fetch_all_reports(uid)

    if payload is None:
        return []

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get(items_field, [])
    else:
        print(
            f"[all-docs] Unexpected payload type: {type(payload).__name__}", file=sys.stderr)
        return []

    if not isinstance(items, list):
        print(
            f"[all-docs] Unexpected `items` type: {type(items).__name__}", file=sys.stderr)
        return []

    return items


# -------------------------
# Fetching (BY COMPANY)
# -------------------------
def iter_company_ids_from_file(path: str) -> Iterator[int]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                yield int(s)
            except ValueError:
                print(f"Skipping non-integer company id: {s}", file=sys.stderr)


def collect_reports_by_company(uid: str,
                               company_ids_file: str,
                               year: Optional[int],
                               items_field: str) -> List[Dict[str, Any]]:
    """
    Return ALL reports across listed companies using fetch_reports_for_company(company_id, uid, year),
    keeping ORIGINAL item shape. Accepts payload as list or dict with `items_field`.
    """
    all_items: List[Dict[str, Any]] = []
    for cid in iter_company_ids_from_file(company_ids_file):
        try:
            data = fetch_reports_for_company(cid, uid, year)
        except TypeError:
            data = fetch_reports_for_company(cid, uid)

        if not data:
            continue

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get(items_field, [])
        else:
            print(
                f"[company {cid}] Unexpected payload type: {type(data).__name__}", file=sys.stderr)
            continue

        if not isinstance(items, list):
            print(
                f"[company {cid}] Unexpected items type: {type(items).__name__}", file=sys.stderr)
            continue

        all_items.extend(items)

    return all_items


# -------------------------
# PDF Validation
# -------------------------
def is_gs_url(url: str) -> bool:
    """
    Treat ONLY 'gs://...' as GCS SDK downloads.
    HTTPS endpoints (storage.googleapis.com / storage.cloud.google.com) are fetched via HTTP.
    """
    try:
        p = urlparse(url)
        return p.scheme == "gs"
    except Exception:
        return False


def validate_pdf_bytes(data: bytes) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Basic PDF integrity checks:
      - magic bytes %PDF
      - parse with pypdf
      - if encrypted, try empty password
    Returns: (ok, reason_if_not, pages_or_None)
    """
    if not data or len(data) < 5:
        return False, "empty_or_too_small", None
    if not data.startswith(b"%PDF"):
        return False, "missing_pdf_magic_bytes", None

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    return False, "encrypted_and_cannot_decrypt", None
            except Exception:
                return False, "encrypted_and_cannot_decrypt", None

        pages = len(reader.pages)
        if pages <= 0:
            return False, "no_pages", None

        return True, None, pages
    except Exception as e:
        return False, f"parse_error:{type(e).__name__}", None


# -------------------------
# Download + Validate (Option A applied)
# -------------------------
def _convert_gs_to_https(url: str) -> str:
    p = urlparse(url)
    bucket = p.netloc
    blob_path = p.path.lstrip("/")
    return f"https://storage.googleapis.com/{bucket}/{blob_path}"


def _read_file_and_cleanup(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    return data


def download_and_validate(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Option A:
      - Use GCS SDK ONLY for gs:// URLs.
      - Use HTTP downloader for https://storage.googleapis.com/... and storage.cloud.google.com/...
      - If SDK fails with Refresh/auth error, fall back to HTTPS.
    """
    try:
        if is_gs_url(url):
            # gs:// -> try SDK first
            try:
                data = download_blob_bytes_from_url(url)
                if not data:
                    return None, "gcs_download_empty"
                return data, None
            except Exception as e:
                # Best-effort detect google auth refresh issues; fall back to HTTPS
                try:
                    from google.auth.exceptions import RefreshError
                    refresh_err = isinstance(e, RefreshError)
                except Exception:
                    refresh_err = False

                https_url = _convert_gs_to_https(
                    url) if refresh_err else _convert_gs_to_https(url)
                path = download_file(https_url)
                if not path or path is False:
                    return None, "http_download_failed_after_gcs_error"
                data = _read_file_and_cleanup(path)
                if not data:
                    return None, "http_download_empty_after_gcs_error"
                return data, None

        # Non-gs URLs -> always HTTP
        path = download_file(url)
        if not path or path is False:
            return None, "http_download_failed"
        data = _read_file_and_cleanup(path)
        if not data:
            return None, "http_download_empty"
        return data, None

    except Exception as e:
        return None, f"download_exception:{type(e).__name__}"


def check_one_item(item: Dict[str, Any], url_field: str) -> Tuple[bool, Optional[str], Optional[int], Dict[str, Any]]:
    """
    Validate one ORIGINAL item:
      - Extract URL from item[url_field]
      - Download & verify PDF
    Returns: (ok, reason, pages, original_item)
    """
    url = str(item.get(url_field, "")).strip()
    if not url:
        return False, "missing_url_field", None, item

    data, err = download_and_validate(url)
    if err or data is None:
        return False, err, None, item

    ok, reason, pages = validate_pdf_bytes(data)
    if ok:
        return True, None, pages, item
    else:
        return False, reason, None, item


# -------------------------
# Main
# -------------------------
def main():
    args = parse_args()
    quiet = args.quiet

    # Get ORIGINAL items (raw dicts)
    if args.mode == "all-docs":
        items = iter_all_reports(uid=args.uid, items_field=args.items_field)
        if not quiet:
            print("Using all-docs mode.", flush=True)
    else:
        if not args.company_ids_file:
            print("by-company mode requires --company-ids-file", file=sys.stderr)
            sys.exit(2)
        items = collect_reports_by_company(
            uid=args.uid,
            company_ids_file=args.company_ids_file,
            year=args.year,
            items_field=args.items_field
        )
        if not quiet:
            print("Using by-company mode.", flush=True)

    # Pull into memory to know totals
    total = len(items)
    if not quiet:
        print(f"Fetched {total} reports from API.", flush=True)

    # EXACT DB/API shape for failures
    bad_original_items: List[Dict[str, Any]] = []
    # same + _status/_reason/_pages
    bad_diagnostics: List[Dict[str, Any]] = []
    good = 0

    from math import ceil
    if not quiet and total:
        print(
            f"Checking with {args.workers} workers (~{ceil(total/max(args.workers, 1))} tasks/worker)...")

    id_field = args.id_field
    url_field = args.url_field

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(check_one_item, item, url_field)
                   for item in items]
        for i, fut in enumerate(as_completed(futures), start=1):
            ok, reason, pages, original_item = fut.result()

            if ok:
                good += 1
                if not quiet and (i <= 10 or i % 250 == 0):
                    print(
                        f"[{i}/{total}] OK so far: {good} | bad: {len(bad_original_items)}")
            else:
                # <-- EXACT DB/API shape
                bad_original_items.append(original_item)
                diag = dict(original_item)
                diag["_status"] = "invalid_or_download_failed"
                diag["_reason"] = reason
                diag["_pages"] = pages
                bad_diagnostics.append(diag)

                if not quiet:
                    rid_for_log = original_item.get(id_field, "<no-id>")
                    url_for_log = (original_item.get(
                        url_field, "") or "")[:120]
                    print(
                        f"[{i}/{total}] BAD id={rid_for_log} reason={reason} url={url_for_log}", flush=True)

    # ---- Outputs ----
    # JSON #1: exact original items for failures (what you need for re-ingestion)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(bad_original_items, f, indent=2, ensure_ascii=False)

    # JSON #2: diagnostics (original + reason/status/pages)
    diag_path = os.path.splitext(args.out_json)[0] + ".diagnostics.json"
    with open(diag_path, "w", encoding="utf-8") as f:
        json.dump(bad_diagnostics, f, indent=2, ensure_ascii=False)

    # CSV: quick scan
    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([id_field, url_field, "status", "reason"])
        for diag in bad_diagnostics:
            w.writerow([diag.get(id_field, ""), diag.get(url_field, ""),
                       diag.get("_status", ""), diag.get("_reason", "")])

    print("\n=== Summary ===")
    print(f"Total checked: {total}")
    print(f"Valid PDFs  : {good}")
    print(f"Failures    : {len(bad_original_items)}")
    print(f"- JSON (original items) : {args.out_json}")
    print(f"- JSON (diagnostics)    : {diag_path}")
    print(f"- CSV                   : {args.out_csv}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
