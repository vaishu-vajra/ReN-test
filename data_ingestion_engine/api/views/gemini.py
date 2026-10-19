import logging

from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status

from data_ingestion_engine.utils.geminiProcessing import (
    collect_and_patch,
    stream_gemini,
    stream_gemini_explain,
    stream_gemini_multi,
)
from data_ingestion_engine.utils.citations import make_lazy_resolver, stream_rewrite
from data_ingestion_engine.utils.store_data import fetch_company_brief, fetch_reports_by_id

from ..auth import FirebaseAuthentication
from rest_framework.permissions import IsAuthenticated
from data_ingestion_engine.utils.permissions import check_ai_access

logger = logging.getLogger(__name__)


def _citation_resolver(docs, uid):
    """
    What the citation rewriter needs to turn a token into a reader link.

    The link carries the company's own name and exchange in its path, and those
    live on the company record rather than the document. That lookup is deferred
    until a citation actually needs it and capped by fetch_company_brief(), so it
    can never sit between the caller and the first byte of the stream: these
    endpoints stream, and a blocking read here surfaces as the endpoint hanging.

    A company that cannot be read resolves to an empty record, which makes
    build_document_link() decline and the citation fall back to a plain page
    reference — an analysis is still worth streaming without links.
    """
    company_id = next((d.get("company_id") for d in docs if d.get("company_id")), None)

    def load_company():
        if not company_id:
            logger.warning("Docs %s carry no company_id — citations will degrade to "
                           "plain page references", [d.get("report_id") for d in docs])
            return None
        return fetch_company_brief(company_id, uid)

    return make_lazy_resolver(load_company, docs)


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def summarise_document_gemini(request):
    """
    Streams a JSON summary of the report using Gemini, and then saves it.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=status.HTTP_400_BAD_REQUEST)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    raw_gen = stream_gemini("summary", doc["document_url"])
    cited_gen = stream_rewrite(raw_gen, _citation_resolver([doc], uid))
    wrapped = collect_and_patch(
        cited_gen, report_id, section="summary", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_data_points_gemini(request):
    """
    Streams extracted data points of the report using Gemini and the 'data-points' schema.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=status.HTTP_400_BAD_REQUEST)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    raw_gen = stream_gemini("data_points", doc["document_url"])
    cited_gen = stream_rewrite(raw_gen, _citation_resolver([doc], uid))
    wrapped = collect_and_patch(
        cited_gen, report_id, section="data_points", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def risk_analysis(request):
    """
    Streams extracted risk_analysis using Gemini.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=status.HTTP_400_BAD_REQUEST)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    raw_gen = stream_gemini("risk_analysis", doc["document_url"])
    cited_gen = stream_rewrite(raw_gen, _citation_resolver([doc], uid))
    wrapped = collect_and_patch(
        cited_gen, report_id, section="risk_analysis", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def priorities_and_sentiment_analysis(request):
    """
    Streams extracted priorities_and_sentiment_analysis using Gemini.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=status.HTTP_400_BAD_REQUEST)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    raw_gen = stream_gemini("priorities_and_sentiment_analysis", doc["document_url"])
    cited_gen = stream_rewrite(raw_gen, _citation_resolver([doc], uid))
    wrapped = collect_and_patch(
        cited_gen, report_id, section="priorities_and_sentiment_analysis", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def gap_analysis(request):
    """
    Streams extracted gap_analysis using Gemini.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_id = request.data.get("report_id")
    if not report_id:
        return Response({"error": "No report_id provided"}, status=status.HTTP_400_BAD_REQUEST)

    doc = fetch_reports_by_id(report_id, uid)
    if not doc:
        return Response({"error": f"No document found for report_id {report_id}"}, status=status.HTTP_404_NOT_FOUND)

    raw_gen = stream_gemini("gap_analysis", doc["document_url"])
    cited_gen = stream_rewrite(raw_gen, _citation_resolver([doc], uid))
    wrapped = collect_and_patch(
        cited_gen, report_id, section="gap_analysis", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


# The frontend sends the trailing window of reports ending at the document the user
# clicked (that report plus the preceding periods). The window is 2-5 documents long:
# quarterly reporters fill all 5, semi-annual (H1/H2) reporters send fewer, and a
# company with only one prior period on file sends just 2. A lone document has no
# baseline to compare against, so it is rejected.
EARNINGS_DELTA_MIN_DOCS = 2
EARNINGS_DELTA_MAX_DOCS = 5


def _period_label(doc: dict) -> str:
    """Human-readable period for a transcript, used in the model's citations."""
    quarter = doc.get("quarter")
    year = doc.get("year")
    period = " ".join(str(p) for p in (quarter, year) if p)
    return period or doc.get("document_title") or "Unknown Period"


def _period_year(doc: dict) -> tuple:
    """(period, year) for a report, tolerating '2025', 2025 and '2025.0'."""
    period = str(doc.get("quarter") or "").strip().upper()
    try:
        year = int(float(doc.get("year")))
    except (TypeError, ValueError):
        year = None
    return period, year


def _periods_per_year(period: str) -> int:
    """How many reports a year this cadence produces: Q1-Q4 -> 4, H1/H2 -> 2, FY -> 1."""
    if period.startswith("Q"):
        return 4
    if period.startswith("H") or period.startswith("S"):
        return 2
    return 1


def _year_ago_index(docs: list) -> int:
    """
    Index of the same period one year before the clicked document. Matches on the
    period label first, so it lands on Q2 2024 for a quarterly reporter and H1 2024
    for a semi-annual one, and falls back to stepping back one reporting year when
    the labels don't line up. Returns -1 when the window doesn't reach that far back.
    """
    current = len(docs) - 1
    period, year = _period_year(docs[current])
    if period and year is not None:
        for index, doc in enumerate(docs[:current]):
            if _period_year(doc) == (period, year - 1):
                return index

    year_ago = current - _periods_per_year(period)
    return year_ago if year_ago >= 0 else -1


def _document_labels(docs: list) -> list:
    """
    `Doc N: Period` labels for the model's citations, annotated with the role each
    report plays in the ReN_Earnings_Delta prompt's three lenses. For a full quarterly
    window the roles land on Doc 1 / Doc 4 / Doc 5 exactly as that prompt assumes; the
    annotations keep the lenses anchored when the window is shorter (H1/H2 reporters)
    or when the year-ago period is missing.
    """
    current = len(docs) - 1
    roles = {current: "CURRENT PERIOD (T_N) — the target transcript for every lens"}
    if current - 1 >= 0:
        roles[current - 1] = "PREVIOUS PERIOD (T_N-1) — the Q-on-Q baseline"

    year_ago = _year_ago_index(docs)
    if year_ago >= 0 and year_ago != current:
        # A semi-annual reporter can have one document fill two roles at once
        existing = roles.get(year_ago)
        role = "PRIOR YEAR COUNTERPART — the Y-on-Y baseline"
        roles[year_ago] = f"{role}; also {existing}" if existing else role

    labels = []
    for index, doc in enumerate(docs):
        label = f"Doc {index + 1}: {_period_label(doc)}"
        role = roles.get(index)
        labels.append(f"{label} [{role}]" if role else label)
    return labels


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def earnings_delta(request):
    """
    Streams the earnings call delta analysis for the report the user clicked.

    Expected payload:
    {
        "report_ids": [<oldest>, ..., <clicked>]   # chronological order, 2-5 reports
    }

    `report_ids` is the trailing window ending at the clicked document — the clicked
    period plus the preceding ones. Quarterly reporters send 5, semi-annual (H1/H2)
    reporters send fewer, and 2 is enough when that is all the history there is.

    Every document goes to Gemini in a single call, and the prompt produces one report
    covering all three comparisons as sections: the multi-period trend, the clicked
    period vs. the same period a year earlier (YoY), and the clicked period vs. the
    one immediately before it (QoQ). Each document is labelled with the role it plays
    so the model knows which report anchors each section; a section whose baseline is
    not in the window has no anchor and comes back thin or empty.

    The single result is saved on the clicked report under the `earnings_delta` section.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    # check_ai_access(request.auth)

    report_ids = request.data.get("report_ids")
    if not isinstance(report_ids, list) or not report_ids:
        return Response(
            {"error": f"report_ids must be a list of {EARNINGS_DELTA_MIN_DOCS}-{EARNINGS_DELTA_MAX_DOCS} "
                      "report ids, oldest first, ending with the clicked document"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Only the most recent window matters, even if the frontend sends more history
    report_ids = report_ids[-EARNINGS_DELTA_MAX_DOCS:]

    if len(report_ids) < EARNINGS_DELTA_MIN_DOCS:
        return Response(
            {"error": f"earnings delta needs at least {EARNINGS_DELTA_MIN_DOCS} reports, "
                      f"got {len(report_ids)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    docs = []
    for report_id in report_ids:
        doc = fetch_reports_by_id(report_id, uid)
        if not doc:
            return Response(
                {"error": f"No document found for report_id {report_id}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not doc.get("document_url"):
            return Response(
                {"error": f"Report {report_id} has no document_url"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        docs.append(doc)

    documents = [
        {"label": label, "url": doc["document_url"]}
        for label, doc in zip(_document_labels(docs), docs)
    ]

    raw_gen = stream_gemini_multi("earnings_delta", documents)
    # `docs` is in the order the model was shown them, so a [[cite:D3 ...]] token
    # resolves to the third transcript in the window rather than the clicked one.
    cited_gen = stream_rewrite(raw_gen, _citation_resolver(docs, uid))
    # Store the single report against the clicked (most recent) transcript
    wrapped = collect_and_patch(
        cited_gen, report_ids[-1], section="earnings_delta", uid=uid)
    return StreamingHttpResponse(wrapped, content_type="application/json")


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def explain_widget(request):
    """
    Streams an explanation for a widget based on the provided JSON payload.
    Expected payload:
    {
        "context": {
            "company_name": "...",
            "ticker_symbol": "...",
            "widget_title": "..."
        },
        "input_json": { ... widget data ... }
    }
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)
    

    # check_ai_access(request.auth)

    context = request.data.get("context")
    input_json = request.data.get("input_json")

    if not context or not input_json:
        return Response({"error": "Missing context or input_json"}, status=status.HTTP_400_BAD_REQUEST)

    # Validate context structure minimally
    if not isinstance(context, dict):
         return Response({"error": "Context must be a dictionary"}, status=status.HTTP_400_BAD_REQUEST)

    raw_gen = stream_gemini_explain(context, input_json)
    # Direct streaming of text
    return StreamingHttpResponse(raw_gen, content_type="text/plain")
