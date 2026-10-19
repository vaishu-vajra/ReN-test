import os
import logging
from collections import defaultdict

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status
from urllib3.exceptions import InsecureRequestWarning
import warnings


from data_ingestion_engine.tasks.downloads import store_docs_for_company_task, chunk_rows
from data_ingestion_engine.utils.gc_service import save_failed_url, download_file_from_gcs
from data_ingestion_engine.utils.ingestion_rows import read_ingestor_rows, is_valid_url
# store_docs_for_company_task not used here


from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated

# Suppress only the InsecureRequestWarning from urllib3
warnings.filterwarnings("ignore", category=InsecureRequestWarning)

BUCKET_NAME = os.getenv('GCS_BUCKET_NAME')


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def download_xls_urls(request):
    s3_key = request.data.get("s3_key")
    if not s3_key:
        return Response({"error": "No S3 key provided"}, status=status.HTTP_400_BAD_REQUEST)

    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    os.makedirs("pdfs", exist_ok=True)
    file_name = os.path.basename(s3_key)
    local_path = os.path.join("pdfs", file_name)
    print(f"Downloading {s3_key} to {local_path}")
    print(f"Using GCS bucket: {BUCKET_NAME}")
    success = download_file_from_gcs(
        BUCKET_NAME, s3_key, download_path=local_path)
    if not success or not os.path.exists(local_path):
        return Response({"error": f"Failed to download or locate {s3_key}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    invalid_urls = []
    all_task_ids = {}

    try:
        rows = read_ingestor_rows(local_path)
        print(f"Read {len(rows)} rows from {file_name}")

        # Queue every row that has a resolvable URL, regardless of file type.
        # The downloader routes each link by type (.pdf → streaming pipeline,
        # .html → Playwright render/scrape), so we no longer pre-filter to PDFs.
        valid_rows = []
        for row in rows:
            # Normalize keys to avoid whitespace/case issues
            row_norm = {str(k).strip(): v for k, v in row.items()}

            link = str(row_norm.get("Link", "")).strip()
            if link.lower() in ("", "nan", "none"):
                continue

            # Basic URL normalization — if missing scheme, assume https
            if not link.startswith(("http://", "https://")):
                link = "https://" + link

            if is_valid_url(link):
                # Ensure the normalized link is back in the row for tasks
                row["Link"] = link
                valid_rows.append(row)
            else:
                invalid_urls.append(link)
                save_failed_url(
                    reason="Invalid format", category="Invalid URLs", company_id=0, metadata=row
                )

        print(f"Found {len(valid_rows)} rows with valid URLs")

        company_map = defaultdict(list)
        for row in valid_rows:
            comp = str(row.get("Company Name", "")).strip()
            company_map[comp].append(row)

        # Pass uid to Celery tasks (no Firebase token). Split each company's docs
        # into small chunks so a company with hundreds of docs becomes many small
        # tasks that each finish well within the worker time limit.
        for company_name, company_rows in company_map.items():
            chunk_ids = []
            for chunk in chunk_rows(company_rows):
                async_res = store_docs_for_company_task.apply_async(
                    args=[chunk, uid, s3_key])
                chunk_ids.append(async_res.id)
            all_task_ids[company_name] = chunk_ids

    except Exception as e:
        logging.error(f"Processing error: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

    return Response(
        {"task_chain_ids_by_company": all_task_ids, "invalid_urls": invalid_urls},
        status=status.HTTP_202_ACCEPTED,
    )
