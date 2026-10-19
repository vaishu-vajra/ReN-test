import os
from data_ingestion_engine.utils.get_pdf_page_count import get_pdf_page_count
from data_ingestion_engine.utils.store_data import patch_report
from .download_file import download_file
import os
from dotenv import load_dotenv

load_dotenv()


def download_update_page_number(report, token):
    """Download a file from a URL, add metadata, upload it to S3, and store the report in the DB."""

    try:

        # Download the PDF file
        local_path = download_file(report['document_url'])
        if not local_path or not os.path.exists(local_path):
            raise Exception(f"Failed to download {report['document_url']}")
        # Get the number of pages
        num_pages = get_pdf_page_count(local_path)
        print(f"The PDF has {num_pages} pages")
        print(type(num_pages))
        print("Report ID:", report['report_id'])
        patch_report(report['report_id'], num_pages, "page_count", token)
        # Clean up the local file
        try:
            os.remove(local_path)
        except OSError as e:
            print(f"Failed to delete local file {local_path}: {e}")

        return local_path, None

    except Exception as e:
        print(f"Error processing download: {str(e)}")
        return None, str(e)
    finally:
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as cleanup_error:
            print(f"Error deleting file {local_path}: {cleanup_error}")
