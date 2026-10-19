import os
import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task

from data_ingestion_engine.utils.download_update_page_number import download_update_page_number
from data_ingestion_engine.utils.store_data import fetch_no_page_num_reports

logger = logging.getLogger(__name__)


@shared_task(soft_time_limit=3000, time_limit=25000)
def update_page_number(uid):
    """
    Download reports missing page numbers and update them.
    Returns { downloaded_files: [local_paths], errors: [str] }.
    """
    downloaded_files = []
    errors = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        # Get the list of URLs from the database (INTERNAL auth via uid)
        reports = fetch_no_page_num_reports(uid)
        if not reports:
            errors.append("No URLs reports in the database.")

        future_to_url = {
            executor.submit(download_update_page_number, report): report
            for report in (reports or [])
        }

        for future in as_completed(future_to_url):
            report = future_to_url[future]
            try:
                result = future.result()
                if result:
                    local_path, error = result
                    if local_path:
                        downloaded_files.append(local_path)
                    if error:
                        errors.append(
                            f"Error processing {report['document_url']}: {error}")
            except SoftTimeLimitExceeded:
                errors.append(
                    f"Timeout exceeded processing {report['document_url']}")
            except Exception as e:
                errors.append(
                    f"Unexpected error processing {report['document_url']}: {str(e)}")

        if os.path.exists('pdfs'):
            shutil.rmtree('pdfs')
            logger.info("Cleaned up the 'pdfs' directory.")

    return {'downloaded_files': downloaded_files, 'errors': errors}
