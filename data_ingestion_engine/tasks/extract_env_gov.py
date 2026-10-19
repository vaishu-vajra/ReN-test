from typing import List, Dict, Any, Optional
import os
from celery import shared_task

from data_ingestion_engine.utils.download_file import download_file
from data_ingestion_engine.utils.gc_service import download_blob_bytes_from_url
from data_ingestion_engine.utils.geminiProcessing import gemini_extract_from_gcs
from data_ingestion_engine.utils.getInstruction import get_instruction
from data_ingestion_engine.utils.normalise_openai_resposes import normalise_openai_data
from data_ingestion_engine.utils.pick_best_doc_per_year import pick_best_docs_by_year
from data_ingestion_engine.utils.store_data import fetch_reports_for_company, store_data
# make sure this is imported


ALLOWED_TYPES_CANON = {
    #    "10-K",
    #    "20-F",
    "Sustainability Report",
    "Annual Report",

}


def _canon_type(val: str) -> str:
    s = (val or "").strip().lower()
    if "annual" in s:
        return "Annual Report"
    if "10-k" in s:
        return "10-K"
    if "20-f" in s:
        return "20-F"
    if "sustainability" in s or "esg" in s:
        return "Sustainability Report"

    return (val or "").strip()


@shared_task(rate_limit="6/m", soft_time_limit=700, time_limit=25000)
def extract_env_gov_for_company(
    company_id: int,
    uid: str,
    target_year: Optional[int] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    requested_year = year if year is not None else target_year

    docs = fetch_reports_for_company(company_id, uid, year=requested_year)
    if not docs:
        return {
            "company_id": company_id,
            "results": [],
            "errors": [f"No documents found for company_id={company_id}"],
            "year": requested_year,
        }

    reports_bucket = docs.get("reports") or []
    filings_bucket = docs.get("filings") or []
    all_docs: List[Dict[str, Any]] = [*reports_bucket, *filings_bucket]

    prepped = []
    for d in all_docs:
        t = _canon_type(d.get("report_type"))
        if t in ALLOWED_TYPES_CANON:
            dd = dict(d)
            dd["report_type"] = t
            prepped.append(dd)

    if not prepped:
        return {
            "company_id": company_id,
            "results": [],
            "errors": [f"No Annual/Sustainability reports for year={requested_year} company_id={company_id}"],
            "year": requested_year,
        }

    results, errors = [], []
    sections = ["environmental"]

    for section in sections:
        docs_for_section = pick_best_docs_by_year(prepped, section)
        if not docs_for_section:
            errors.append(
                f"No doc for '{section}' in year={requested_year} company_id={company_id}")
            continue

        for doc in docs_for_section:
            report_id = doc.get("report_id")
            year_to_store = requested_year or doc.get("year")
            local_path = None

            try:
                gcs_uri = doc["document_url"]
                print(f"[{section}] Using doc:", {
                    "report_id": report_id,
                    "report_type": doc.get("report_type"),
                    "year": year_to_store,
                    "gcs_uri": gcs_uri,
                })

                # Download (handles both GCS and HTTP URLs)
                pdf_bytes = None

                try:
                    pdf_bytes = download_blob_bytes_from_url(gcs_uri)
                except Exception:
                    # fallback to local file if non-GCS URL
                    local_path = download_file(gcs_uri)
                    if not local_path or not os.path.exists(local_path):
                        raise ValueError(
                            f"Failed to download file from {gcs_uri}")
                    with open(local_path, "rb") as f:
                        pdf_bytes = f.read()

                if not pdf_bytes:
                    raise ValueError("Downloaded file is empty or unreadable")

                prompt = get_instruction(section)
                extracted = gemini_extract_from_gcs(section, prompt, gcs_uri)
                normalized = normalise_openai_data(extracted)

                ok = store_data(section, normalized,
                                year_to_store, company_id, uid)
                if ok:
                    results.append(
                        f"Extracted '{section}' from report_id={report_id} (year={year_to_store})")
                else:
                    errors.append(
                        f"Store failed for '{section}', report_id={report_id} (year={year_to_store})")

            except Exception as e:
                errors.append(
                    f"Extraction error for '{section}', report_id={report_id}: {e}")

            finally:
                if local_path and os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                        print(f"Cleaned up: {local_path}")
                    except Exception as cleanup_err:
                        print(
                            f"Warning: failed to delete {local_path}: {cleanup_err}")

    return {
        "company_id": company_id,
        "results": results,
        "errors": errors,
        "year": requested_year,
    }
