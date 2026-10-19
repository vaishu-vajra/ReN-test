import os
from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status

from data_ingestion_engine.utils.OpenAISummariseDocument import stream_response
from data_ingestion_engine.utils.store_data import fetch_reports_by_id
from data_ingestion_engine.utils.download_file import download_file

from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated
from data_ingestion_engine.utils.permissions import check_ai_access


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def summarise_document(request):
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=400)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    local_path = download_file(doc["document_url"])
    if not local_path or not os.path.exists(local_path):
        return Response({"error": "Failed to download the document"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    stream_generator = stream_response(
        uid=uid,
        instruction="summary",
        secondary_prompt="Please provide the same summary from the uploaded document.",
        file_path=local_path,
        company_id=doc.get("company_id"),
        report_id=report_id,
    )
    return StreamingHttpResponse(stream_generator, content_type="text/markdown")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_data_points(request):
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=400)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    local_path = download_file(doc["document_url"])
    if not local_path or not os.path.exists(local_path):
        return Response({"error": "Failed to download the document"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    stream_generator = stream_response(
        uid=uid,
        instruction="data_points",
        secondary_prompt="Please provide the same data points from the uploaded document.",
        file_path=local_path,
        company_id=doc.get("company_id"),
        report_id=report_id,
    )
    return StreamingHttpResponse(stream_generator, content_type="text/markdown")
