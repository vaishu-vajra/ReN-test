import os
import time
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def cleanup_old_files():
    """
    Deletes files in the 'pdfs' directory that are older than 2 days.
    """
    directory = 'pdfs'
    if not os.path.exists(directory):
        logger.info(f"Directory '{directory}' does not exist.")
        return

    now = time.time()
    cutoff = now - (2 * 24 * 60 * 60)  # 2 days

    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        if os.path.isfile(file_path):
            file_mtime = os.path.getmtime(file_path)
            if file_mtime < cutoff:
                try:
                    os.remove(file_path)
                    logger.info(f"Deleted old file: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")
