import logging
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status

from data_ingestion_engine.utils.gc_service import get_failed_items
from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated


@api_view(["GET"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def get_failed_urls(request):
    """
    Return failed-URL records filtered by an optional Unix-timestamp range.

    Query params (all optional):
        start_date: Unix epoch seconds — lower bound on `timestamp` (inclusive)
        end_date:   Unix epoch seconds — upper bound (inclusive)

    Omitting `end_date` returns everything ≥ start_date.
    Omitting both returns the whole collection.
    """
    start_date = request.query_params.get("start_date") or None
    end_date = request.query_params.get("end_date") or None

    try:
        failed_items = get_failed_items(start_date=start_date, end_date=end_date)
        return Response(failed_items, status=status.HTTP_200_OK)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logging.error(f"Failed to get items: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
