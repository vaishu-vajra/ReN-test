import json
import os
import time
import uuid
from data_ingestion_engine.utils.coerce_data import _coerce_year
from data_ingestion_engine.utils.compress_pdf import compress_pdf, compress_pdf_aggressive
from data_ingestion_engine.utils.gc_service import upload_to_gcs
from data_ingestion_engine.utils.get_pdf_page_count import get_pdf_page_count
from data_ingestion_engine.utils.sanitizeFilename import sanitize_filename
from data_ingestion_engine.utils.store_data import get_company_id, store_ingesting_report, store_report
from .gc_service import download_file_from_gcs, save_failed_url
from .download_file import download_file, DownloadError
from .handle_metadata_value import handle_metadata_value
from dotenv import load_dotenv

load_dotenv()


# Hard ceiling on ingested PDF size. Files larger than this break the
# downstream RAG indexing pipeline, so we reject them outright (after the
# lossless compression step has had a chance to salvage borderline files).
MAX_PDF_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB


# ---------------------------------------------------------------------------
# Processing-pipeline error classes — each carries a `category` that flows
# into MongoDB via save_failed_url, mirroring the DownloadError hierarchy
# in download_file.py.  This lets us tell *which stage* failed when triaging.
# ---------------------------------------------------------------------------

class ProcessingError(Exception):
    """Base for failures in the post-download processing pipeline."""
    category = "Failed URLs"


class InvalidPDFError(ProcessingError):
    """Downloaded file is not a usable PDF (page count <= 0)."""
    category = "Invalid PDF"


class FileTooLargeError(ProcessingError):
    """PDF exceeds the size cap that downstream RAG indexing can handle."""
    category = "File Too Large"


class CompanyResolutionError(ProcessingError):
    """DataX could not resolve / upsert the company."""
    category = "Company Resolution Failed"


class GCSUploadError(ProcessingError):
    """File downloaded successfully but upload to Google Cloud Storage failed."""
    category = "GCS Upload Failed"


class DataXStoreError(ProcessingError):
    """DataX rejected the company-documents insert."""
    category = "DataX Store Failed"


class DataXIngestionError(ProcessingError):
    """DataX rejected the ingestion-reports insert (report did store, ingestion record didn't)."""
    category = "DataX Ingestion Failed"


class MetadataLookupError(ProcessingError):
    """No metadata row found for the URL in the provided json_data."""
    category = "Metadata Missing"


def process_download(url, json_data, uid):
    try:
        metadata = next(
            (item for item in json_data if item['Link'] == url), None)
        if not metadata:
            raise MetadataLookupError(f"No metadata found for URL: {url}")

        # --- BASIC METADATA NORMALIZATION ---

        raw_title = metadata.get('Title of the Report')
        raw_year = metadata.get('Year', None)
        raw_type = metadata.get('Type of Report')
        raw_name = metadata.get('Company Name')
        raw_tickers = metadata.get('Ticker Symbol')
        raw_quarter = metadata.get('Quarter', None)

        title = handle_metadata_value(raw_title)
        company_name = handle_metadata_value(raw_name)
        type_of_report = handle_metadata_value(raw_type)
        if type_of_report == 'Earnings Call':
            type_of_report = 'earnings_call_transcript'
            metadata['Type of Report'] = type_of_report

        # Coerce year; fallback uses title + url
        fallback_text = f"{title} {url}"
        year_int = _coerce_year(
            raw_year,
            fallback_text=fallback_text,
            default=1900
        )
        metadata['Year'] = year_int
        if raw_quarter:
            metadata['Quarter'] = raw_quarter

        # --- TICKER / EXCHANGE / SECTOR PARSING ---

        # The T:EXCH ticker convention has been dropped. The ticker cell now holds
        # only symbol(s) (dots kept, e.g. "ADM.L"), and the exchange comes from its
        # own dedicated column. This lets us disambiguate companies that share a
        # ticker across different exchanges.
        tickers = parse_ticker_symbols(raw_tickers)
        primary_ticker = tickers[0] if tickers else ""
        exchange = handle_metadata_value(metadata.get('Exchange'))
        sector = handle_metadata_value(metadata.get('Sector'))

        # Put normalized values into metadata for downstream code
        metadata['PrimaryTicker'] = primary_ticker
        metadata['Exchange'] = exchange
        metadata['Sector'] = sector
        metadata['AllTickers'] = tickers

        # --- FILENAME & LOCAL PATH ---

        unsanitized_file_name = (
            f"{company_name}_{title}_{type_of_report}_{year_int}.pdf"
            .replace(' ', '_')
            .replace('/', '|')
        )
        sanitized_file_name = sanitize_filename(unsanitized_file_name)
        s3_key = f"pdfs/{sanitized_file_name}"
        # Unique local filename prevents race conditions when concurrent workers
        # process files that sanitize to the same name.
        unique_local_name = f"{sanitized_file_name[:-4]}_{uuid.uuid4().hex[:8]}.pdf"
        local_path = os.path.join('pdfs', unique_local_name)

        # --- DOWNLOAD PDF ---
        # download_file raises a DownloadError subclass on failure.
        # Each subclass carries a `.category` used by save_failed_url below.
        download_file(url, local_path)
        if not os.path.exists(local_path):
            raise DownloadError(f"Downloaded file missing at path: {local_path}")

        # --- PAGE COUNT ---

        num_pages = get_pdf_page_count(local_path)
        print(f"The PDF has {num_pages} pages")
        if num_pages <= 0:
            raise InvalidPDFError(
                f"Downloaded file is not a valid PDF (page count: {num_pages}): {url}"
            )

        # --- COMPRESS PDF (lossless, in-place) ---
        # Failures are non-fatal: original file is kept and ingestion continues.
        compress_pdf(local_path)

        # --- ENFORCE SIZE CAP ---
        # If lossless compression didn't get us under the cap, escalate to
        # lossy Ghostscript presets (least → most aggressive). Reject only if
        # even /screen quality can't make it fit.
        file_size = os.path.getsize(local_path)
        if file_size > MAX_PDF_SIZE_BYTES:
            logger_msg_size_mb = file_size / (1024 * 1024)
            print(
                f"Lossless output still {logger_msg_size_mb:.1f}MB — "
                f"escalating to lossy compression for {url}"
            )
            agg_result = compress_pdf_aggressive(local_path, MAX_PDF_SIZE_BYTES)
            file_size = os.path.getsize(local_path)
            if not agg_result["under_target"]:
                raise FileTooLargeError(
                    f"PDF exceeds {MAX_PDF_SIZE_BYTES // (1024 * 1024)}MB cap "
                    f"({file_size / (1024 * 1024):.1f}MB after lossy "
                    f"compression): {url}"
                )
            # Page count can change if gs re-flowed pages — refresh it.
            num_pages = get_pdf_page_count(local_path)
            if num_pages <= 0:
                raise InvalidPDFError(
                    f"PDF unreadable after lossy compression: {url}"
                )

        # --- RESOLVE COMPANY ---

        company_id = get_company_id(
            metadata=metadata,
            uid=uid,
            ticker=primary_ticker,
            exchange=exchange,
            tickers=tickers,
        )

        if not company_id:
            raise CompanyResolutionError(
                f"Could not resolve company '{metadata.get('Company Name', '')}' "
                f"(ticker={primary_ticker!r}, isin={metadata.get('ISIN', '')!r}) for URL: {url}"
            )

        # --- PREPARE METADATA FOR GCS ---

        metadata_args = {
            'x-amz-meta-industry': handle_metadata_value(metadata.get('Industry', '')),
            'x-amz-meta-sector': sector,
            'x-amz-meta-company-name': company_name,
            'x-amz-meta-company-id': handle_metadata_value(company_id),
            'x-amz-meta-link': handle_metadata_value(metadata.get('Link', '')),
            'x-amz-meta-ISIN': handle_metadata_value(metadata.get('ISIN', '')),
            # keep the raw cell value so we don't lose info
            'x-amz-meta-Ticker-Symbol': handle_metadata_value(raw_tickers),
            # plus normalized primary ticker + exchange (from the Exchange column)
            'x-amz-meta-Primary-Ticker': handle_metadata_value(primary_ticker),
            'x-amz-meta-Primary-Exchange': exchange,
            'x-amz-meta-year': str(year_int),
            'x-amz-meta-country': handle_metadata_value(metadata.get('Country', '')),
            'x-amz-meta-document-type': handle_metadata_value(metadata.get('Type of Document')),
            'x-amz-meta-document-title': title,
            'x-amz-meta-report-type': handle_metadata_value(type_of_report),
            'x-amz-meta-quarter': handle_metadata_value(raw_quarter),
        }

        # --- UPLOAD TO GCS ---

        gcs_url, upload_error = upload_to_gcs(
            local_path,
            s3_key,
            metadata_args
        )
        if upload_error:
            raise GCSUploadError(f"GCS upload failed for {url}: {upload_error}")
        print(f"Uploaded file to GCS: {gcs_url}")

        # --- STORE REPORT IN DB ---

        store_report_result, store_report_error = store_report(
            metadata,
            company_id=company_id,
            num_pages=num_pages,
            gcs_url=gcs_url,
            uid=uid
        )
        if not store_report_result:
            raise DataXStoreError(
                f"store_report failed for {url} — DataX: {store_report_error}"
            )

        store_ingestion_report_result, store_ingestion_error = store_ingesting_report(
            metadata,
            company_id=company_id,
            gcs_url=gcs_url,
            uid=uid
        )
        if not store_ingestion_report_result:
            raise DataXIngestionError(
                f"store_ingesting_report failed for {url} — DataX: {store_ingestion_error}"
            )

        # --- CLEANUP LOCAL FILE ---

        try:
            os.remove(local_path)
        except OSError as e:
            print(f"Failed to delete local file {local_path}: {e}")

        return s3_key, None

    except Exception as e:
        print(f"Error processing download: {str(e)}")
        if 'company_id' not in locals():
            company_id = 0
        fail_metadata = metadata if 'metadata' in locals() else {}
        # Use the typed exception's category if available (DownloadError subclasses),
        # otherwise fall back to the generic bucket.
        failure_category = getattr(e, 'category', 'Failed URLs')
        save_failed_url(
            reason=str(e),
            category=failure_category,
            company_id=company_id,
            metadata=fail_metadata
        )
        return None, str(e)
    finally:
        try:
            if 'local_path' in locals() and os.path.exists(local_path):
                os.remove(local_path)
        except Exception as cleanup_error:
            print(f"Error deleting file {local_path}: {cleanup_error}")


def parse_ticker_symbols(raw_value):
    """
    Split a ticker cell into individual symbols.

    The T:EXCH convention has been dropped — the exchange now lives in its own
    column, so this only splits on commas and keeps each symbol verbatim (dots
    are part of the symbol, e.g. "ADM.L", "PIA.MI").

    Input examples:
        "ADM.L"            -> ["ADM.L"]
        "BREE, BREE.L"     -> ["BREE", "BREE.L"]

    Returns a list of ticker strings (possibly empty).
    """
    if not raw_value:
        return []

    value = handle_metadata_value(raw_value)
    return [x.strip() for x in value.split(",") if x.strip()]
