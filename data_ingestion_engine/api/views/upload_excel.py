import os
import time
import uuid
import tempfile

from django.utils.text import slugify
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.request import Request

from rest_framework import status
from rest_framework.permissions import IsAuthenticated

# Adjust this import to wherever you defined FirebaseAuthentication

# Reuse your existing helper that uploads a *local file path* to GCS
# Signature expected: upload_to_gcs(local_path: str, gcs_key: str, metadata: dict) -> (gcs_url, error)
from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.utils.gc_service import upload_to_gcs

BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")


ALLOWED_EXTS = {".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"}


def _safe_ext(name: str) -> str:
    base = (name or "").lower()
    for ext in ALLOWED_EXTS:
        if base.endswith(ext):
            return ext
    return ""


def _timestamp() -> str:
    # seconds resolution is fine; if you want more uniqueness add uuid
    return time.strftime("%Y%m%d%H%M%S")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def upload_excel_to_gcs(request: Request):
    """
    Accepts an Excel file via multipart/form-data and uploads it to GCS.
    Response: {"key": "...", "url": "..."}
    """
    if not BUCKET_NAME:
        return Response({"error": "GCS_BUCKET_NAME is not configured"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    f = request.FILES.get("file")
    if not f:
        return Response({"error": "Missing 'file' in request.FILES"}, status=status.HTTP_400_BAD_REQUEST)

    ext = _safe_ext(f.name)
    if not ext:
        return Response({"error": "Unsupported file type. Allowed: .xlsx, .xls, .xlsm, .xlsb, .csv"}, status=status.HTTP_400_BAD_REQUEST)

    # Build "<filename_timestamp.extension>"
    base_no_ext = f.name.rsplit(".", 1)[0]
    # Keep it short & safe
    safe_base = slugify(base_no_ext) or "excel"
    ts = _timestamp()
    final_name = f"{safe_base}_{ts}{ext}"

    # Optional: make it even more unique if you expect name collisions in the same second
    # final_name = f"{safe_base}_{ts}_{uuid.uuid4().hex[:8]}{ext}"

    # We’ll upload under "excels/<filename_timestamp.ext>"
    gcs_key = f"excel_urls/{final_name}"

    # Write the uploaded file to a temp file, since your helper expects a local path
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            for chunk in f.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        # Optional metadata (handy for later debugging)
        metadata = {
            "x-amz-meta-original-filename": f.name,
            "x-amz-meta-content-type": f.content_type or "application/vnd.ms-excel",
            "x-amz-meta-uploaded-by": getattr(request.user, "uid", "") or "unknown",
        }

        gcs_url, upload_error = upload_to_gcs(tmp_path, gcs_key, metadata)
        if upload_error:
            return Response({"error": f"Upload failed: {upload_error}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"key": gcs_key, "url": gcs_url}, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    finally:
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
