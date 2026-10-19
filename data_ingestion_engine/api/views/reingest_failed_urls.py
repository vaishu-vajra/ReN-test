import logging

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from data_ingestion_engine.tasks.downloads import reingest_failed_urls_task
from ..auth import FirebaseAuthentication

logger = logging.getLogger(__name__)

# Maximum failed_urls items packed into a single Celery message.
# Keeps per-task Redis payload small (~250 KB at 500 × ~500-byte docs) so
# apply_async stays fast and the request returns before gunicorn's worker
# timeout. Override per request via the optional `chunk_size` body field.
DEFAULT_CHUNK_SIZE = 500


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def reingest_failed_urls(request):
    """
    Dispatch Celery task(s) that re-ingest failed URLs.

    Request body (all optional — pick ONE input mode):

      Mode A — fetch from MongoDB (single coordinator task):
          {
              "start_date": "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS",
              "end_date":   "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS",
              "ids":        ["<mongo_id>", ...]
          }

      Mode B — caller-provided list (chunked into multiple tasks):
          {
              "failed_urls": [ {<failed_url_doc>}, ... ],
              "chunk_size":  500           // optional, default 500
          }

    For Mode B, the array is split into chunks and one Celery task is
    dispatched per chunk. This prevents a single huge Redis message and
    keeps the HTTP request fast even with 10k+ URLs.
    """
    start_date = request.data.get("start_date")
    end_date = request.data.get("end_date")
    ids = request.data.get("ids") or None
    failed_urls = request.data.get("failed_urls")
    chunk_size_raw = request.data.get("chunk_size") or DEFAULT_CHUNK_SIZE

    # ── Validate failed_urls input ─────────────────────────────────────────
    if failed_urls is not None and not isinstance(failed_urls, list):
        return Response(
            {"error": "`failed_urls` must be a JSON array of failed_url objects."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        chunk_size = int(chunk_size_raw)
        if chunk_size < 1:
            raise ValueError
    except (TypeError, ValueError):
        return Response(
            {"error": "`chunk_size` must be a positive integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Mode B — caller-provided list, chunked ─────────────────────────────
    if failed_urls:
        total = len(failed_urls)
        dispatched = []
        for offset in range(0, total, chunk_size):
            chunk = failed_urls[offset:offset + chunk_size]
            res = reingest_failed_urls_task.apply_async(
                kwargs={"failed_urls": chunk}
            )
            dispatched.append({
                "chunk_index": offset // chunk_size,
                "chunk_size": len(chunk),
                "task_id": res.id,
            })

        logger.info(
            "Dispatched %d reingest chunks (total %d URLs, chunk_size=%d)",
            len(dispatched), total, chunk_size,
        )
        return Response(
            {
                "source": "request",
                "total_urls": total,
                "chunk_size": chunk_size,
                "chunks_dispatched": len(dispatched),
                "tasks": dispatched,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    # ── Mode A — MongoDB fetch, single coordinator task ────────────────────
    res = reingest_failed_urls_task.apply_async(
        kwargs={
            "ids": ids,
            "start_date": start_date,
            "end_date": end_date,
        }
    )

    logger.info(
        "Dispatched reingest_failed_urls_task id=%s source=mongodb start=%s end=%s",
        res.id, start_date, end_date,
    )
    return Response(
        {
            "task_id": res.id,
            "source": "mongodb",
            "start_date": start_date,
            "end_date": end_date,
        },
        status=status.HTTP_202_ACCEPTED,
    )
