import logging
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status

from data_ingestion_engine.tasks.page_numbers import update_page_number


from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def update_page_num(request):
    try:
        uid = getattr(request.user, "uid", None)
        if not uid:
            return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

        update_page_number.apply_async(args=[uid])
        return Response({"message": "Page number update initiated successfully"}, status=status.HTTP_200_OK)
    except Exception as e:
        logging.error(f"Update error: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
