# data_ingestion_engine/api/views/dial_global/ingestion.py
import os
import logging
from collections import defaultdict

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status
from urllib3.exceptions import InsecureRequestWarning
import warnings

from data_ingestion_engine.utils.gc_service import save_failed_url, download_file_from_gcs
from data_ingestion_engine.utils.ingestion_rows import (
    read_ingestor_rows_dial_global,
    is_valid_url,
    get_row_value,
    normalise_row_keys,
    normalize_dial_global_document_type,
)

from data_ingestion_engine.tasks.dial_global.dial_global_downloads import (
    store_docs_for_company_task_dial_global,
)
from data_ingestion_engine.tasks.downloads import chunk_rows  # shared chunking helper, reused as-is

from ...auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')

# The 3 canonical DIAL Global document types (see utils/ingestion_rows.py for the
# normalization map). Anything that doesn't match one of these after normalization
# is still processed (not silently dropped) but is worth watching in logs/failures.
DIAL_GLOBAL_KNOWN_DOCUMENT_TYPES = {"Annual Report", "Sustainability Report", "DEI Report"}


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def download_xls_urls_dial_global(request):
    """
    POST /dial-global/download-xls-urls/

    Same request contract as the existing /download-xls-urls/ endpoint:
        { "s3_key": "..." }

    Same 202 response shape:
        { "task_chain_ids_by_company": {...}, "invalid_urls": [...] }

    Differences from the main endpoint:
    - Reads a multi-tab DIAL Global tracking workbook (one tab per intern) instead
      of a single sheet, via read_ingestor_rows_dial_global().
    - Expects columns "Company Name", "Document Type", "Year",
      "Document URL (Google drive)" instead of "Company Name" / "Link".
    - "Notes & Questions" column and intern/tab name are intentionally ignored —
      not parsed, not stored (per DIAL Global project decision).
    - Document Type values are normalized via normalize_dial_global_document_type()
      to the 3 canonical types before being queued.
    - Failed URLs are saved with label="dial_global" (see utils/gc_service.py) so
      they're filterable separately from the main pipeline's failures.
    """
    s3_key = request.data.get("s3_key")
    if not s3_key:
        return Response({"error": "No S3 key provided"}, status=status.HTTP_400_BAD_REQUEST)

    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    os.makedirs("pdfs", exist_ok=True)
    file_name = os.path.basename(s3_key)
    local_path = os.path.join("pdfs", file_name)
    logging.info(f"[dial_global] Downloading {s3_key} to {local_path}")
    logging.info(f"[dial_global] Using GCS bucket: {BUCKET_NAME}")

    success = download_file_from_gcs(
        BUCKET_NAME, s3_key, download_path=local_path)
    if not success or not os.path.exists(local_path):
        return Response(
            {"error": f"Failed to download or locate {s3_key}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    invalid_urls = []
    all_task_ids = {}

    try:
        rows = read_ingestor_rows_dial_global(local_path)
        logging.info(f"[dial_global] Read {len(rows)} rows (across all tabs) from {file_name}")

        valid_rows = []
        skipped_no_year = []
        for row in rows:
            # Normalize keys to avoid whitespace/case issues, same pattern as the
            # main pipeline's endpoint. The live sheet ships "Document Type "
            # with a trailing space, so this is load-bearing.
            row_norm = normalise_row_keys(row)

            link = get_row_value(row_norm, "document_url")
            if not link:
                continue

            if not link.startswith(("http://", "https://")):
                link = "https://" + link

            company_name = get_row_value(row_norm, "company_name")
            if not company_name:
                # No company name — can't attribute this row to anything, skip it
                # the same way an empty link is skipped.
                continue

            # The curated year is what the extraction engine scores against, so
            # a row without one is worth surfacing rather than ingesting blind.
            year = get_row_value(row_norm, "year")
            if not year:
                skipped_no_year.append(
                    {"company_name": company_name, "document_url": link})

            if is_valid_url(link):
                normalized_row = {
                    "Company Name": company_name,
                    "Document Type": normalize_dial_global_document_type(
                        get_row_value(row_norm, "document_type")
                    ),
                    "Year": year,
                    "Document URL (Google drive)": link,
                }
                valid_rows.append(normalized_row)
            else:
                invalid_urls.append(link)
                save_failed_url(
                    reason="Invalid format",
                    category="Invalid URLs",
                    company_id=0,
                    metadata=row_norm,
                    label="dial_global",
                )

        logging.info(f"[dial_global] Found {len(valid_rows)} rows with valid URLs")

        company_map = defaultdict(list)
        for row in valid_rows:
            company_map[row["Company Name"]].append(row)

        for company_name, company_rows in company_map.items():
            chunk_ids = []
            for chunk in chunk_rows(company_rows):
                async_res = store_docs_for_company_task_dial_global.apply_async(
                    args=[chunk, uid, s3_key])
                chunk_ids.append(async_res.id)
            all_task_ids[company_name] = chunk_ids

        logging.info(
            "[dial_global] Queued %s companies / %s documents from %s",
            len(company_map), len(valid_rows), file_name)

    except Exception as e:
        logging.error(f"[dial_global] Processing error: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

    return Response(
        {
            "task_chain_ids_by_company": all_task_ids,
            "invalid_urls": invalid_urls,
            "total_companies": len(all_task_ids),
            "total_documents": len(valid_rows),
            # Rows that will ingest but can never be extracted until the sheet
            # gets a year — the extraction engine refuses to guess one.
            "rows_missing_year": skipped_no_year,
        },
        status=status.HTTP_202_ACCEPTED,
    )
