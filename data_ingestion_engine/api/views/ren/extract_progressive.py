# data_ingestion_engine/api/views/ren/extract_progressive.py
#
# Fan-out for the PROGRESSIVE ReN pipeline.
#
# Differs from extract_all.py in what the unit of work is. That endpoint takes a
# cohort of COMPANIES and runs each one's most recent `years` company-years.
# This one takes a list of specific (company_id, year) PAIRS and runs exactly
# those, because its cohort is a leaderboard export filtered on score rather
# than a company list.
#
# The pairs come from one of two places, in order of precedence:
#   1. "pairs" in the request       — explicit, no parsing
#   2. "csv_path" / "s3_key"        — a scored export, filtered on max_score
#
# There is no name or ISIN resolution anywhere in here, deliberately. The ids in
# a leaderboard export are already canonical companies.company_id values;
# re-resolving them would risk landing on the duplicate empty twin that the
# ISIN-only strategy in company_list.py exists to avoid.

import logging
import os
from collections import Counter
from typing import Any, Dict, List, Tuple

from celery import group
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from data_ingestion_engine.api.auth import FirebaseAuthentication
from data_ingestion_engine.tasks.ren.extract import (
    DEFAULT_YEARS, extract_ren_progressive_for_company)
from data_ingestion_engine.utils.gc_service import download_file_from_gcs
from data_ingestion_engine.utils.ren.company_list import load_flagged_pairs

logger = logging.getLogger(__name__)

# Smaller than the joint pipeline's 40. A progressive task is several sequential
# model calls rather than one, so a batch occupies its Celery slots for much
# longer and a smaller batch gives the queue more chances to interleave.
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_SCORE = 70.0

# Above this many pairs, a cohort-sourced (non-explicit) run must echo the
# exact count back in `confirm`. Small runs and hand-listed pairs go straight
# through — the guard is for "run the whole sheet", which is the one that can
# overwrite hundreds of production rows from a three-word request body.
CONFIRM_THRESHOLD = 25

# Pairs echoed back on a dry run before truncating. 797 of them is ~10k lines
# of JSON, which buries the counts and the warning above it.
PAIR_PREVIEW_LIMIT = 50

BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")

# Which cohort a bare request (no `pairs`, no `csv_path`) runs.
#
# This is the CURRENT target, not a permanent one — it moves each time a cohort
# is finished. Previous cohorts stay in the repo and stay runnable by passing
# `csv_path` explicitly; nothing here is deleted when the default changes.
#
#   2026-08-26  "Below 70 - below 70.csv"
#               308 company-years / 252 companies, mean baseline 56.9. Unlike
#               the cohort before it, every row already holds 2-3 documents —
#               these did not score low for want of a second report, so the
#               gain has to come from the six report types added on 2026-08-26
#               and from gap-filling across them.
#   2026-08-25  "Problem Companies Full List - Sheet2.csv"  [DONE]
#               797 company-years / 567 companies, every one scored on exactly
#               one document. Disjoint from the cohort above — no pair appears
#               in both, so re-running one cannot overwrite the other.
DEFAULT_COHORT_CSV = os.getenv(
    "REN_FLAGGED_PAIRS_CSV",
    "data_ingestion_engine/Below 70 - below 70.csv")


def chunked(seq: List[Any], size: int) -> List[List[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def parse_pairs(raw: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Normalise an explicit "pairs" body into cohort entries.

    Accepts [[company_id, year], ...] and [{"company_id":…, "year":…}, ...]
    interchangeably. Returns (pairs, rejected).
    """
    pairs: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen = set()

    for entry in raw or []:
        try:
            if isinstance(entry, dict):
                company_id = int(entry["company_id"])
                year = int(entry["year"])
                name = entry.get("company_name")
            else:
                company_id, year = int(entry[0]), int(entry[1])
                name = entry[2] if len(entry) > 2 else None
        except (KeyError, IndexError, TypeError, ValueError):
            rejected.append({"entry": str(entry)[:200],
                             "reason": "expected [company_id, year] or "
                                       "{company_id, year}"})
            continue

        if (company_id, year) in seen:
            continue
        seen.add((company_id, year))
        pairs.append({"company_id": company_id, "year": year,
                      "company_name": name, "baseline_score": None})

    return pairs, rejected


@api_view(["POST"])
@authentication_classes([FirebaseAuthentication])
@permission_classes([IsAuthenticated])
def extract_ren_progressive(request):
    """
    POST /ren/extract/progressive/

    Re-runs specific ReN company-years through the progressive engine: eleven
    report types instead of four, every document of each type, and one model
    call per REPORT TYPE with the unanswered questions carried forward. Every
    annual report goes into one call together, every pay gap report into
    another, and so on.

    POST body (all optional):
    {
      "pairs": [[25, 2024], [58, 2026]],   // explicit company-years
      "csv_path": "…/Problem Companies…csv",  // or read them from an export
      "s3_key": "cohorts/problem-companies.csv",  // same, from GCS
      "max_score": 70,          // only rows at or below this score
      "batch_size": 20,
      "dry_run": false,         // resolve and report, dispatch nothing
      "include_all_pairs": false,  // dry run: echo all pairs, not the first 50
      "max_per_type": null,     // documents of one type per year (0 = unlimited)
      "max_per_year": null      // hard cap on documents per year
    }

    ALWAYS dry_run first. It reports how many pairs parsed, without spending a
    model call. It does NOT report the call count: that depends on how many
    report types each company-year holds, which only
    scripts/audit_ren_document_types.py can answer.

    This OVERWRITES the stored row for each pair: storage upserts on
    (ren_company_id, year), so the existing scored result is replaced in place.
    Snapshot the current rows before a real run — see
    scripts/snapshot_ren_baseline.py.
    """
    uid = getattr(request.user, "uid", None)
    if not uid:
        return Response({"error": "Missing uid"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        batch_size = int(request.data.get("batch_size", DEFAULT_BATCH_SIZE))
        if batch_size <= 0:
            batch_size = DEFAULT_BATCH_SIZE
    except Exception:
        batch_size = DEFAULT_BATCH_SIZE

    try:
        max_score = float(request.data.get("max_score", DEFAULT_MAX_SCORE))
    except Exception:
        return Response({"error": "Invalid 'max_score' value"},
                        status=status.HTTP_400_BAD_REQUEST)

    def _optional_int(field):
        value = request.data.get(field)
        return int(value) if value not in (None, "") else None

    try:
        max_per_type = _optional_int("max_per_type")
        max_per_year = _optional_int("max_per_year")
    except Exception:
        return Response({"error": "max_per_type and max_per_year must be integers"},
                        status=status.HTTP_400_BAD_REQUEST)

    dry_run = str(request.data.get("dry_run", False)).lower() in ("1", "true", "yes")
    include_all_pairs = str(
        request.data.get("include_all_pairs", False)).lower() in ("1", "true", "yes")

    try:
        years = int(request.data.get("years", DEFAULT_YEARS))
        if years <= 0:
            years = DEFAULT_YEARS
    except Exception:
        return Response({"error": "'years' must be a positive integer"},
                        status=status.HTTP_400_BAD_REQUEST)

    skipped: List[Dict[str, Any]] = []
    local_path = None

    # ---- 1. explicit pairs ----
    if request.data.get("pairs"):
        pairs, skipped = parse_pairs(request.data["pairs"])
        source = "pairs"

    # ---- 1b. company ids, no years ----
    #
    # A cohort that has never been scored has no (company_id, year) pairs to
    # name — there is no prior row to point at. These run the company's most
    # recent `years` reporting years instead, exactly as /ren/extract/all/ does,
    # by dispatching with year=None and letting selection pick them.
    #
    # Ids only, never names or tickers: resolution is a separate, reviewable
    # step (scripts/resolve_company_cohort.py) because a resolution that
    # succeeds is not the same as one that is right, and the duplicate-twin
    # problem is not something to discover mid-run.
    elif request.data.get("company_ids"):
        try:
            ids, seen = [], set()
            for value in request.data["company_ids"]:
                company_id = int(value)
                if company_id not in seen:
                    seen.add(company_id)
                    ids.append(company_id)
        except (TypeError, ValueError):
            return Response({"error": "company_ids must be a list of integers"},
                            status=status.HTTP_400_BAD_REQUEST)
        pairs = [{"company_id": company_id, "year": None,
                  "company_name": None, "baseline_score": None}
                 for company_id in ids]
        source = "company_ids"

    # ---- 2. a scored export, local or in GCS ----
    else:
        s3_key = request.data.get("s3_key")
        if s3_key:
            os.makedirs("pdfs", exist_ok=True)
            local_path = os.path.join("pdfs", os.path.basename(s3_key))
            ok = download_file_from_gcs(BUCKET_NAME, s3_key, download_path=local_path)
            if not ok or not os.path.exists(local_path):
                return Response({"error": f"Failed to download or locate {s3_key}"},
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            csv_path, source = local_path, f"s3_key:{s3_key}"
        else:
            csv_path = request.data.get("csv_path") or DEFAULT_COHORT_CSV
            source = f"csv_path:{csv_path}"

        if not os.path.exists(csv_path):
            return Response(
                {"error": f"Cohort file not found: {csv_path}. Pass 'pairs' "
                          f"explicitly, or 'csv_path'/'s3_key'."},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            pairs, skipped = load_flagged_pairs(csv_path, max_score=max_score)
        except Exception as e:
            logger.error("[ren-progressive] Failed to parse %s: %s", csv_path, e)
            return Response({"error": f"Failed to parse cohort: {e}"},
                            status=status.HTTP_400_BAD_REQUEST)
        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)

    if not pairs:
        return Response(
            {"message": "No company-years to run.", "source": source,
             "total_pairs": 0, "skipped_rows": len(skipped),
             "skipped": skipped[:50]},
            status=status.HTTP_200_OK)

    companies = sorted({p["company_id"] for p in pairs})
    summary: Dict[str, Any] = {
        "source": source,
        "mode": "progressive",
        "total_pairs": len(pairs),
        "total_companies": len(companies),
        # Only meaningful when the cohort came from a scored export.
        "max_score": max_score if source not in ("pairs", "company_ids") else None,
        "years": years,
        "max_per_type": max_per_type,
        "max_per_year": max_per_year,
        "skipped_rows": len(skipped),
        "skipped": skipped[:50],
    }

    if dry_run:
        # With `pairs` the unit IS a company-year. With `company_ids` it is a
        # COMPANY, each of which runs up to `years` company-years — saying
        # "275 company-years" there understates the run by a factor of `years`,
        # which is exactly the number someone sizes the cost against.
        if source == "company_ids":
            scale = (f"{len(companies)} company/companies, each running its "
                     f"{years} most recent reporting year(s) — so up to "
                     f"{len(companies) * years} company-years")
        else:
            scale = (f"{len(pairs)} company-year(s) across "
                     f"{len(companies)} company/companies")
        summary["message"] = (
            f"Dry run — nothing dispatched. {scale}. Each company-year costs "
            f"one extraction call per REPORT TYPE read plus one scoring call, "
            f"so the total depends on how many types each year holds — run "
            f"scripts/audit_ren_document_types.py for that distribution. "
            f"NOTE: a real run REPLACES the stored row for every company-year.")

        # Year spread is worth seeing before dispatching: a cohort can reach
        # back to 2021 and forward to 2027, and a reporting year in the future
        # is usually a bad `year` on the document rather than a real filing.
        #
        # Deliberately NOT named `years` — that is the int argument above, and
        # shadowing it here would send a Counter to the task.
        year_counts = Counter(p["year"] for p in pairs if p["year"] is not None)
        summary["pairs_by_year"] = (
            dict(sorted(year_counts.items(), reverse=True)) if year_counts
            else f"not fixed — each company runs its {years} most recent "
                 f"reporting year(s)")

        all_pairs = [[p["company_id"], p["year"]] for p in pairs]
        if include_all_pairs or len(all_pairs) <= PAIR_PREVIEW_LIMIT:
            summary["pairs"] = all_pairs
        else:
            # 797 pairs is ~10k lines of JSON and buries everything above it.
            # Capped like `skipped` is; pass include_all_pairs for the full list.
            summary["pairs"] = all_pairs[:PAIR_PREVIEW_LIMIT]
            summary["pairs_truncated"] = True
            summary["pairs_shown"] = PAIR_PREVIEW_LIMIT
            summary["pairs_hint"] = (
                'Pass {"include_all_pairs": true} for all '
                f'{len(all_pairs)}.')
        return Response(summary, status=status.HTTP_200_OK)

    # ---- guard: a whole-cohort run needs a second, explicit signal ----
    #
    # `dry_run` defaults to False, so the destructive path is the one you get by
    # omitting a field. That is the wrong way round for an operation that
    # REPLACES stored rows in place with no history: a body as small as
    # {"max_score": 70} would otherwise overwrite every flagged company-year.
    #
    # Explicit `pairs` are exempt — listing company-years by hand is already a
    # deliberate act, and that is the shape a pilot run takes.
    if source != "pairs" and len(pairs) > CONFIRM_THRESHOLD:
        confirm = str(request.data.get("confirm", "")).strip()
        if confirm != str(len(pairs)):
            return Response(
                {
                    "error": "Confirmation required for a whole-cohort run.",
                    "detail": (
                        f"This would dispatch {len(pairs)} company-year(s) and "
                        f"REPLACE the stored row for every one of them. Storage "
                        f"upserts on (ren_company_id, year) and DataX keeps no "
                        f"history."),
                    "how_to_proceed": {
                        "inspect_first": {"dry_run": True},
                        "pilot_a_few": {"pairs": [[25, 2024], [25, 2025]]},
                        "really_run_all": {"confirm": str(len(pairs))},
                    },
                    "snapshot_first": (
                        "python -m data_ingestion_engine.scripts."
                        "snapshot_ren_baseline --uid <uid> --out baseline.json"),
                    "total_pairs": len(pairs),
                    "total_companies": len(companies),
                },
                status=status.HTTP_400_BAD_REQUEST)

    batches = chunked(pairs, batch_size)
    batch_group_infos: List[Dict[str, Any]] = []
    for index, batch in enumerate(batches):
        signatures = [
            extract_ren_progressive_for_company.s(
                pair["company_id"], uid,
                year=pair["year"],
                # Only consulted when year is None — a pair names its own year.
                years=years,
                company_name=pair["company_name"],
                max_per_type=max_per_type,
                max_per_year=max_per_year)
            for pair in batch
        ]
        async_res = group(signatures).apply_async()
        batch_group_infos.append({
            "batch_index": index,
            "group_id": async_res.id,
            "pairs": [[p["company_id"], p["year"]] for p in batch],
            "count": len(batch),
        })

    summary.update({
        "message": "Scheduled progressive ReN extraction for all batches.",
        "batch_size": batch_size,
        "num_batches": len(batches),
        "batches": batch_group_infos,
    })
    return Response(summary, status=status.HTTP_202_ACCEPTED)
