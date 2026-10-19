import os
import logging
import uuid
from typing import Dict, Any

# Import shared utilities from the main pipeline (do not fork them)
from data_ingestion_engine.utils.process_download import (
    download_file, 
    get_pdf_page_count, 
    compress_pdf, 
    compress_pdf_aggressive,
    MAX_PDF_SIZE_BYTES, 
    sanitize_filename, 
    handle_metadata_value,
    parse_ticker_symbols, 
    DownloadError, 
    ProcessingError
)
from data_ingestion_engine.utils.gc_service import upload_to_gcs

# Import the new Dial Global store_data functions we just created
from data_ingestion_engine.utils.dial_global.store_data import (
    get_company_id_dial_global,
    store_report_dial_global
)

logger = logging.getLogger(__name__)

def process_download_dial_global(company_name: str, row: Dict[str, Any], uid: str) -> Dict[str, Any]:
    """
    Downloads, compresses, uploads to GCS, and saves to DataX specifically for DIAL Global.
    """
    # Key names must match what the DIAL Global ingestion view emits in
    # normalized_row (api/views/dial_global/ingestion.py) — these three values
    # are curated by hand in the tracking workbook and are the trusted source
    # for the document's URL, type and reporting year. Reading the wrong key
    # here silently drops a curated value into a NULL column downstream.
    url = row.get("Document URL (Google drive)") or row.get("Link")
    if not url:
        raise ProcessingError("No URL provided in spreadsheet row")

    year = row.get("Year")
    doc_type = row.get("Document Type") or row.get("Type of Report") or "Report"

    logger.info(f"[DIAL Global] Processing {company_name} - {year} - {url}")

    # 1. Download File
    pdf_path = download_file(url)
    
    try:
        # 2. Get pages and compress if needed (reusing exact main pipeline logic)
        page_count = get_pdf_page_count(pdf_path)
        file_size = os.path.getsize(pdf_path)
        
        if file_size > MAX_PDF_SIZE_BYTES:
            logger.info(f"File too large ({file_size} bytes). Compressing...")
            pdf_path = compress_pdf(pdf_path)
            if os.path.getsize(pdf_path) > MAX_PDF_SIZE_BYTES:
                pdf_path = compress_pdf_aggressive(pdf_path)

        # 3. Prepare GCS Upload with DIAL Global specific prefixes and metadata
        clean_name = sanitize_filename(f"{company_name}_{year}_{doc_type}")
        # THIS IS THE CRITICAL PREFIX CHANGE FOR DIAL GLOBAL
        gcs_key = f"dial_global/pdfs/{clean_name}_{uuid.uuid4().hex[:8]}.pdf"
        
        # Add the specific label to metadata
        metadata = {
            "company_name": handle_metadata_value(company_name),
            "year": handle_metadata_value(year),
            "document_type": handle_metadata_value(doc_type),
            "source_url": handle_metadata_value(url),
            "x-amz-meta-source-label": "dial_global"
        }
        
        # Add tickers if they exist in the row
        tickers = parse_ticker_symbols(row.get("Ticker Symbol", ""))
        if tickers:
            metadata["tickers"] = handle_metadata_value(",".join(tickers))

        # Upload to Google Cloud Storage. upload_to_gcs takes the metadata dict
        # positionally as metadata_args and returns (public_url, error) — both
        # easy to get wrong, and both silent: the wrong keyword raises, and
        # unpacked wrongly the tuple lands in document_url as a string.
        gcs_url, upload_error = upload_to_gcs(pdf_path, gcs_key, metadata)
        if upload_error or not gcs_url:
            raise ProcessingError(
                f"GCS upload failed for {url}: {upload_error}")

        # 4. Save to DataX via DIAL Global endpoints
        company_id = get_company_id_dial_global(company_name, uid)
        if not company_id:
            # Upsert failed, or a 409 could not be resolved back to a row.
            # Without an id the report row cannot be attributed, so fail here
            # with the reason rather than letting store_report_dial_global
            # raise on int(None) further down.
            raise ProcessingError(
                f"Could not resolve a DIAL Global company_id for '{company_name}'")

        doc_data = {
            "title": f"{company_name} {year} {doc_type}",
            "year": year,
            "document_type": doc_type,
            "source_url": url,
            "gcs_url": gcs_url
        }
        
        report_id = store_report_dial_global(company_id, doc_data, uid)
        
        logger.info(f"[DIAL Global] Successfully processed {company_name} -> Report ID: {report_id}")
        
        return {
            "status": "success", 
            "gcs_url": gcs_url, 
            "report_id": report_id,
            "page_count": page_count
        }
        
    finally:
        # Always clean up the local downloaded file to prevent disk fill-up
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception as e:
                logger.warning(f"Failed to clean up temp file {pdf_path}: {e}")
