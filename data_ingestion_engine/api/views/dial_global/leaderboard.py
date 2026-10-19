# data_ingestion_engine/api/views/dial_global/leaderboard.py
#
# Download the DIAL Global scores as a leaderboard spreadsheet — the same
# flattening the offline script does, served over HTTP so nobody has to run a
# script to get the current sheet.

import io
import logging
from datetime import datetime

import pandas as pd
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.utils.dial_global.export import fetch_workbook

logger = logging.getLogger(__name__)

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@api_view(["GET"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def dial_global_leaderboard(request):
    """
    GET /dial-global/leaderboard/[?format=xlsx|csv|json][&sheet=Scoring|All]

    xlsx returns a workbook with both sheets:
      Scoring — the scoring JSON: rubric level, weight, weighted score,
                rationale and uncertainty flags per category, plus the total
      All     — the extraction JSON: profile and baseline metrics, then a
                score/justification pair for every question Q16-Q79

    csv holds a single table, so it takes ?sheet= (default Scores).
    json returns both sheets keyed by name.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    fmt = (request.query_params.get("format") or "xlsx").lower()
    if fmt not in ("xlsx", "csv", "json"):
        return Response(
            {"error": "format must be one of: xlsx, csv, json"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sheet = request.query_params.get("sheet") or "Scoring"

    try:
        workbook = fetch_workbook(uid)
    except Exception as e:
        logger.error("[dial_global] Leaderboard build failed: %s", e)
        return Response(
            {"error": f"Failed to build leaderboard: {e}"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if fmt == "json":
        return Response(
            {name: {"count": len(f), "rows": f.to_dict(orient="records")}
             for name, f in workbook.items()},
            status=status.HTTP_200_OK,
        )

    if sheet not in workbook:
        return Response(
            {"error": f"Unknown sheet '{sheet}'. Available: {list(workbook)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    stamp = datetime.utcnow().strftime("%Y%m%d")
    filename = f"dial_global_{stamp}.{fmt}"

    if fmt == "csv":
        payload = workbook[sheet].to_csv(index=False).encode("utf-8")
        response = HttpResponse(payload, content_type="text/csv")
        filename = f"dial_global_{sheet.lower()}_{stamp}.csv"
    else:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            for name, frame in workbook.items():
                frame.to_excel(writer, index=False, sheet_name=name)
        response = HttpResponse(buffer.getvalue(),
                                content_type=XLSX_CONTENT_TYPE)

    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
