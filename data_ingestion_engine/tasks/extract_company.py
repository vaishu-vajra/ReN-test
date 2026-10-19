import logging
from celery import shared_task

from data_ingestion_engine.utils.geminiProcessing import gemini_extract_from_gcs
from data_ingestion_engine.utils.getInstruction import get_instruction
from data_ingestion_engine.utils.normalise_openai_resposes import normalise_openai_data
from data_ingestion_engine.utils.pick_best_doc_per_year import pick_best_docs_by_year
from data_ingestion_engine.utils.store_data import (
    delete_ingestion_reports_for_company,
    fetch_ingestion_reports_for_company,
    get_company_id,
    store_data,
)

logger = logging.getLogger(__name__)


@shared_task(soft_time_limit=950, time_limit=25000)
def extract_docs_for_company_task(store_result, company_rows, uid):
    """
    After docs are stored, extract structured JSON per section and persist it.
    Auth is INTERNAL via uid (no Firebase token in workers).
    """
    results = []
    errors = []

    # Propagate any storage errors
    if isinstance(store_result, dict) and store_result.get("errors"):
        errors.extend(store_result["errors"])

    if not company_rows:
        return {"error": "No rows for this company."}

    # Identify company
    company_name = company_rows[0].get("Company Name", "").strip()
    company_id = get_company_id(company_rows[0], uid)
    if not company_id:
        return {"error": f"Failed to get company_id for {company_name}"}

    # Fetch all ingested docs for this company
    docs = fetch_ingestion_reports_for_company(company_id, uid)
    logger.info(
        f"Found {docs} ingested docs for {company_name} (company_id={company_id})")

    # Sections to extract
    sections = [
        "overview and key-metrics",
        "key-personal",
        "key-initiatives",
        "key-priorities",
        "dimensions-summary",
        "board-members",
        # "social",
        # "environmental",
        # "governance",
    ]

    for section in sections:
        docs_for_section = pick_best_docs_by_year(docs, section)
        if not docs_for_section:
            errors.append(f"No doc found for '{section}' for {company_name}.")
            continue

        for doc in docs_for_section:
            report_id = doc.get("report_id")
            year = doc.get("year") or 1900
            try:
                gcs_uri = doc["document_url"]
                logger.info(
                    f"Processing {section} for report_id={report_id}, year={year}")

                prompt = get_instruction(section)
                logger.info(f"Using prompt for {section}: {prompt}")
                extracted = gemini_extract_from_gcs(
                    section, prompt, gcs_uri)
                logger.debug(f"Extracted data for {section}: {extracted}")
                normalized = normalise_openai_data(extracted)
                ok = store_data(section, normalized,
                                int(year), company_id, uid)

                if not ok:
                    errors.append(
                        f"Error storing '{section}' for report_id={report_id} (year={year})"
                    )
                else:
                    results.append(
                        f"Extracted '{section}' from report_id={report_id} (year={year})"
                    )

            except Exception as e:
                print(e)
                errors.append(
                    f"Gemini extraction error for '{section}', report_id={report_id}: {e}"
                )

    delete_ingestion_reports_for_company(company_id, uid)
    return {"company_name": company_name, "results": results, "errors": errors}
