# data_ingestion_engine/tasks/company_year_engine.py
#
# The shared two-pass CITIES engine, driven by both extraction pipelines:
#
#   tasks/dial_global/extract.py — DGI-collected companies, sourced from the
#       dial_global_* tables.
#   tasks/ren/extract.py         — ReN companies, sourced from the canonical
#       companies / company-documents tables.
#
# The two pipelines differ only in where documents come from, how documents are
# selected, and where results are stored. Everything between those points — the
# company-year grouping, the two model calls, the curated-year stamping, the
# score arithmetic, the counters and the log record — lives here so the two can
# never drift apart.
#
#   Pass 1 (dial_global_extract) — reads a company-year's PDFs together and
#     extracts Q1-Q79 into DialGlobalExtractionSchema.
#   Pass 2 (dial_global_score) — reads the pass-1 JSON, with no PDFs, and scores
#     it against the CITIES rubric into DialGlobalScoringSchema.
#
# Both results are stored together. If pass 2 fails, the pass-1 extraction is
# still stored, flagged unscored.

import logging
import os
import statistics
import tempfile
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from google.genai import types

from data_ingestion_engine.schemas.dial_global_schemas import (
    CATEGORY_WEIGHTS,
    DIAL_GLOBAL_SCHEMA_MAP,
    QUESTION_CATALOGUE,
    DialGlobalExtractionSchema,
    DialGlobalExtractionWire,
    DialGlobalScoringSchema,
    OrganisationMetadataWire,
    QuestionAnswer,
    assemble_extraction,
    finalise_scoring,
    render_question_catalogue,
    stamp_reporting_year,
)
from data_ingestion_engine.utils.dial_global.store_data import (
    STATUS_SCORED,
    STATUS_UNSCORED,
)
from data_ingestion_engine.utils.compress_pdf import (
    compress_pdf,
    compress_pdf_aggressive,
)
from data_ingestion_engine.utils.download_file import download_file
from data_ingestion_engine.utils.gc_service import (
    download_blob_bytes_from_url,
    save_extraction_log,
)
from data_ingestion_engine.utils.geminiProcessing import (
    TOTAL_INLINE_LIMIT_BYTES,
    delete_uploaded_files,
    gemini_extract_from_gcs,
    make_thinking_config,
    get_genai_client,
    make_pdf_part,
)
from data_ingestion_engine.utils.getInstruction import get_instruction_with_meta

logger = logging.getLogger(__name__)

# Section keys — also the Langfuse prompt names, and the keys into
# DIAL_GLOBAL_SCHEMA_MAP. Both pipelines score against the same rubric, so they
# share both prompts.
EXTRACT_SECTION = "dial_global_extract"
SCORE_SECTION = "dial_global_score"

# Both passes run on Flash by default. Pass 2 is split out so it can be pointed
# at a stronger model without touching pass 1 — it is the judgement-heavy call
# and its output drives the published score.
EXTRACT_MODEL = os.getenv("DIAL_GLOBAL_EXTRACT_MODEL", "gemini-3.8-flash")
SCORE_MODEL = os.getenv("DIAL_GLOBAL_SCORE_MODEL", "gemini-3.8-flash")

# Ceiling on how many PDFs go into one extraction call. A company-year is
# normally 1-4 documents; this only guards against a curation mistake dumping a
# whole archive into one year and blowing the context window.
MAX_DOCS_PER_CALL = int(os.getenv("DIAL_GLOBAL_MAX_DOCS_PER_CALL", "6"))

# Output budget for pass 1. 64 answers with verbatim justifications is a lot of
# tokens, and on 2.5 the model's thinking draws on the SAME budget — leave it at
# the default and a long extraction gets truncated mid-JSON, which surfaces as a
# response with no text part at all.
EXTRACT_MAX_OUTPUT_TOKENS = int(
    os.getenv("DIAL_GLOBAL_EXTRACT_MAX_OUTPUT_TOKENS", "32768"))

# Gemini caps a single PDF at 50 MB regardless of how it is delivered — the
# Files API raises the transport limit to 2 GB but the model still refuses the
# document with 400 INVALID_ARGUMENT. Anything above this is compressed first.
GEMINI_PDF_MAX_BYTES = int(
    os.getenv("GEMINI_PDF_MAX_BYTES", str(45 * 1024 * 1024)))
SCORE_MAX_OUTPUT_TOKENS = int(
    os.getenv("DIAL_GLOBAL_SCORE_MAX_OUTPUT_TOKENS", "16384"))

# How many times each company-year is scored. One run: the model's output is
# taken as given.
#
# Raising this to 3 turns on consensus scoring — the runs are compared, the
# majority rubric level per category is published, and any category the runs
# disagree on is flagged for review. That was measured on this data: repeated
# runs over an IDENTICAL extraction moved a category by a full rubric level on
# 18 of 79 ReN company-years, and Accenture 2024 returned equity levels of
# 5, 3 and 5 across three runs. Scoring is the cheap text-only pass (~7.5k input
# tokens against 74k-210k for extraction) and the runs go out concurrently, so
# three costs roughly 12% more tokens and almost no wall time.
#
# At 1 the scoring_consensus payload is null and the Scoring sheet's score_runs,
# disagreeing_levels and score_spread columns are empty, so human_review_required
# reflects only the flags the model raises about itself.
SCORE_RUNS = int(os.getenv("DIAL_GLOBAL_SCORE_RUNS", "1"))


def load_pdf_bytes(document_url: str) -> bytes:
    """
    Fetch one document.

    GCS first (that is where both ingestion paths upload to), falling back to a
    plain HTTP download for anything else, same as the env/gov task.
    """
    local_path = None
    try:
        try:
            return download_blob_bytes_from_url(document_url)
        except Exception:
            local_path = download_file(document_url)
            if not local_path or not os.path.exists(local_path):
                raise ValueError(f"Failed to download file from {document_url}")
            with open(local_path, "rb") as f:
                return f.read()
    finally:
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception as cleanup_err:
                logger.warning("Failed to delete %s: %s",
                               local_path, cleanup_err)


def fit_pdf_for_gemini(pdf_bytes: bytes, label: str) -> bytes:
    """
    Shrink a PDF under Gemini's 50 MB per-file ceiling, or raise.

    Lossless repacking first; Ghostscript downsampling only if that is not
    enough, since it degrades the scanned pages the extraction depends on.
    Raises when neither gets under the limit — better a named failure than a
    request the model will reject with an opaque 400.
    """
    if len(pdf_bytes) <= GEMINI_PDF_MAX_BYTES:
        return pdf_bytes

    original = len(pdf_bytes)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(pdf_bytes)
        tmp.close()

        try:
            compress_pdf(tmp.name)
        except Exception as e:
            logger.warning("Lossless compression failed for %s: %s", label, e)

        # compress_pdf_aggressive REPORTS failure, it does not raise: a missing
        # Ghostscript binary comes back as {"error": ...} with the file
        # untouched. Discarding the result made "gs is not installed", "gs timed
        # out" and "gs tried every preset and the file is genuinely
        # incompressible" produce one identical message — which is not enough to
        # act on when a document has just cost a whole company-year.
        outcome = {}
        if os.path.getsize(tmp.name) > GEMINI_PDF_MAX_BYTES:
            try:
                outcome = compress_pdf_aggressive(
                    tmp.name, target_bytes=GEMINI_PDF_MAX_BYTES) or {}
            except Exception as e:
                outcome = {"error": f"raised: {e}"}
                logger.warning("Aggressive compression failed for %s: %s", label, e)

        with open(tmp.name, "rb") as f:
            shrunk = f.read()
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

    if len(shrunk) > GEMINI_PDF_MAX_BYTES:
        if outcome.get("error"):
            why = f"Ghostscript did not run: {outcome['error']}"
        elif outcome.get("preset_used"):
            why = (f"Ghostscript reached preset {outcome['preset_used']} and "
                   f"still produced {outcome.get('compressed_size', 0) / 1e6:.1f} MB")
        else:
            why = ("Ghostscript ran through every preset without getting under "
                   "the limit — the file is probably a large scan")
        raise RuntimeError(
            f"{label} is {len(shrunk) / 1e6:.1f} MB after compression "
            f"(from {original / 1e6:.1f} MB) and Gemini caps a PDF at "
            f"{GEMINI_PDF_MAX_BYTES / 1e6:.0f} MB. {why}")

    logger.info("Compressed %s from %.1f MB to %.1f MB to fit Gemini's PDF cap",
                label, original / 1e6, len(shrunk) / 1e6)
    return shrunk


def curated_year(doc: Dict[str, Any], requested_year: Optional[int] = None) -> Optional[int]:
    """
    The reporting year we trust for a document, or None.

    The year comes from the ingestion source (the DIAL Global tracking workbook,
    or the canonical company_documents row) — never from the model, because
    annual reports are full of prior-year comparatives and picking the wrong
    column is an easy mistake to make.
    """
    for candidate in (doc.get("year"), requested_year):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def group_docs_by_year(
    docs: List[Dict[str, Any]],
    requested_year: Optional[int] = None,
) -> tuple:
    """
    Bucket a company's documents into company-years, newest year first.

    Returns (ordered {year: [docs]}, undated_docs). Documents with no curated
    year go into `undated` rather than being guessed at — the caller reports
    them so the gap in the source data gets filled.
    """
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    undated: List[Dict[str, Any]] = []

    for doc in docs:
        year = curated_year(doc, requested_year)
        if year is None:
            undated.append(doc)
            continue
        buckets.setdefault(year, []).append(doc)

    ordered = OrderedDict(
        (year, buckets[year]) for year in sorted(buckets, reverse=True))
    return ordered, undated


def _document_label(doc: Dict[str, Any], index: int) -> str:
    """Human-readable label so the model can attribute a citation to a source."""
    doc_type = doc.get("document_type") or "Document"
    return f"Document {index}: {doc_type} ({doc.get('year')})"


def _run_extraction_pass(
    documents: List[Dict[str, Any]],
    reporting_year: int,
) -> tuple:
    """
    Pass 1 — Q1-Q79 extraction over ALL of a company's documents for one year.

    Each PDF is preceded by a label part so the model can say which document a
    quote came from, and the curated reporting year is supplied as context so
    figures are read from the right column of multi-year tables.

    Returns (extraction, prompt_meta, documents_used).
    """
    prompt, meta = get_instruction_with_meta(EXTRACT_SECTION)

    contents: List[Any] = [
        f"REPORTING YEAR: {reporting_year}. This is curated data, not something "
        f"to verify — it is the financial year these documents report on. Where "
        f"a document presents several years side by side, extract the figures "
        f"for this year.\n\n"
        f"You are given {len(documents)} document(s) for this company-year, "
        f"labelled below. Treat them as ONE combined body of evidence: a "
        f"disclosure missing from one document may be present in another, and a "
        f"question should be scored 1 if ANY of them supports it. Attribute each "
        f"quote to the document it came from by name in the justification.",

        # The response schema asks for a flat `answers` array rather than 64
        # named fields, so the question text lives here instead of in the
        # schema descriptions. Emit one entry per question below, using the
        # question_id verbatim.
        "QUESTION CATALOGUE — answer every one of these in `answers`, using the "
        "exact question_id shown before the '::'.\n\n"
        + render_question_catalogue(),
    ]

    client = get_genai_client()
    documents_used = []
    uploaded = []
    # Fetch everything first so the combined size is known before deciding how
    # to deliver it: a set that is fine document-by-document can still be too
    # much to inline in one request.
    payloads = []
    for doc in documents:
        report_id = doc.get("report_id")
        try:
            pdf_bytes = load_pdf_bytes(doc["document_url"])
        except Exception as e:
            raise RuntimeError(
                f"Could not fetch report_id={report_id} "
                f"({doc.get('document_type')}): {e}") from e
        if not pdf_bytes:
            raise RuntimeError(
                f"Downloaded document is empty or unreadable: report_id={report_id}")
        try:
            pdf_bytes = fit_pdf_for_gemini(
                pdf_bytes, f"report_id={report_id} ({doc.get('document_type')})")
        except Exception as e:
            raise RuntimeError(f"Could not fit report_id={report_id}: {e}") from e
        payloads.append(pdf_bytes)

    force_upload = sum(len(b) for b in payloads) > TOTAL_INLINE_LIMIT_BYTES

    try:
        for index, (doc, pdf_bytes) in enumerate(zip(documents, payloads), start=1):
            # Every document of the company-year must make it into the call.
            # A partial set would still extract and score, and the score would
            # look perfectly reasonable while resting on less evidence than it
            # claims — so any failure here fails the whole company-year.
            report_id = doc.get("report_id")
            label = _document_label(doc, index)
            try:
                # Oversized PDFs go through the File API — inlining them is
                # rejected as prompt_block=BlockedReason.OTHER, which reads like
                # a content filter rather than a size limit.
                part, uploaded_file = make_pdf_part(
                    pdf_bytes, label, client=client, force_upload=force_upload)
            except Exception as e:
                raise RuntimeError(
                    f"Could not attach report_id={report_id} "
                    f"({doc.get('document_type')}, {len(pdf_bytes) / 1e6:.1f} MB): {e}") from e

            if uploaded_file is not None:
                uploaded.append(uploaded_file)

            contents.append(f"--- {label} ---")
            contents.append(part)
            documents_used.append({
                "report_id": report_id,
                "document_type": doc.get("document_type"),
                "year": doc.get("year"),
                "document_url": doc.get("document_url"),
                "label": label,
                "size_bytes": len(pdf_bytes),
                "delivery": "file_api" if uploaded_file is not None else "inline",
            })

        if len(documents_used) != len(documents):
            raise RuntimeError(
                f"Only {len(documents_used)} of {len(documents)} documents were "
                f"attached — refusing to extract on a partial document set")

        try:
            wire = gemini_extract_from_gcs(
                EXTRACT_SECTION,
                prompt,
                None,              # PDFs are supplied as labelled extra parts
                schema_map=DIAL_GLOBAL_SCHEMA_MAP,   # -> DialGlobalExtractionWire
                model=EXTRACT_MODEL,
                extra_contents=contents,
                max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
                thinking=make_thinking_config(EXTRACT_MODEL),
            )
        except Exception as e:
            # Name the documents that were in the call. A prompt block is
            # usually one document's content rather than the set, and without
            # the manifest there is no way to tell which one to look at.
            manifest = "; ".join(
                f"report_id={d['report_id']} {d['document_type']} "
                f"{d['size_bytes'] / 1e6:.1f}MB via {d['delivery']}"
                for d in documents_used)
            raise RuntimeError(f"{e} | documents in call: {manifest}") from e
    finally:
        # Uploads are only needed for the duration of the call.
        delete_uploaded_files(uploaded, client=client)

    # Fold the flat answers array back into the contract shape, then insert the
    # curated year as q02_year — which is what the scoring prompt reads.
    result = assemble_extraction(wire)
    result = stamp_reporting_year(result, reporting_year)
    return result, meta, documents_used


# ---------------------------------------------------------------------------
# Pass 1, progressive — one call per document CATEGORY, gaps carried forward
# ---------------------------------------------------------------------------
#
# The joint pass above reads a whole company-year in one call. That caps how
# much evidence a row can rest on: a 1,155-page document set exceeds the
# 1,048,576-token prompt limit outright, and widening the document types feeding
# it makes that worse rather than better.
#
# The progressive pass splits that one call into one call PER REPORT TYPE. Every
# annual report goes into the first call together, every sustainability report
# into the second, and so on down REN_ROUND_ORDER. Each call is asked ONLY the
# questions no earlier round answered, so the residual set — and the prompt —
# shrinks as the rounds go on.
#
# The category, not the document, is the unit for a reason. A company that files
# UK, Ireland and France gender pay gap reports separately has published ONE body
# of evidence in three files: a question answered by combining them is a real
# answer, and splitting them across three calls would lose it. Same for a
# two-volume annual report. Reading a type together is also what lets the model
# attribute a figure to the right jurisdiction instead of guessing.
#
# The merge is deliberately monotone: only score == 1 settles a question. A null
# from round K means "not in these documents", not "not disclosed", so the
# question stays in the residual for K+1. A later round can therefore only ADD a
# positive — it can never retract one an earlier round established.
#
# That makes this pass strictly more permissive than the joint one. A question
# the joint call would leave null because the surrounding context contradicted it
# can be set by a weaker document read in isolation, so progressive scores are
# not apples-to-apples with joint ones and should not be mixed in one ranking
# without saying so.

# Upper bound on rounds per company-year. With one round per report type this can
# only bind at 11 (the length of REN_ROUND_ORDER), so it is a backstop rather
# than a live constraint — it matters if the type list ever grows.
MAX_ROUNDS_PER_YEAR = int(os.getenv("DIAL_GLOBAL_MAX_ROUNDS_PER_YEAR", "20"))

# Documents in ONE round, i.e. one report type's worth. Grouping by category
# re-introduces the multi-PDF context pressure the per-document design avoided,
# so this is the guard: a company with nine annual reports filed under one year
# is a curation error, not evidence worth 900 pages of context.
MAX_DOCS_PER_ROUND = int(os.getenv("DIAL_GLOBAL_MAX_DOCS_PER_ROUND", "6"))


def _run_extraction_round(
    prompt: str,
    docs: List[Dict[str, Any]],
    round_number: int,
    total_rounds: int,
    document_type: str,
    reporting_year: int,
    residual: List[str],
    part: Optional[tuple] = None,
) -> tuple:
    """
    One progressive round: every document of ONE report type, one model call.

    All of `docs` share a canonical report type and are read together as a single
    body of evidence — three jurisdictional pay gap reports answer a question
    between them that none answers alone.

    A document that cannot be fetched or attached is skipped and reported rather
    than failing the round, so one dead URL does not cost the round its other
    documents. The round fails only when NOTHING attached.

    Returns (wire, attached, skipped) where `attached` describes the documents
    that made it into the call and `skipped` the ones that did not.
    """
    # Fetch everything first: a set that is fine document-by-document can still
    # be too much to inline in one request.
    payloads: List[tuple] = []
    skipped: List[Dict[str, Any]] = []

    for doc in docs:
        report_id = doc.get("report_id")
        try:
            pdf_bytes = load_pdf_bytes(doc["document_url"])
            if not pdf_bytes:
                raise RuntimeError("downloaded document is empty or unreadable")
            pdf_bytes = fit_pdf_for_gemini(
                pdf_bytes, f"report_id={report_id} ({document_type})")
        except Exception as e:
            skipped.append({"report_id": report_id, "reason": str(e)})
            logger.warning("[progressive] round %s: skipping report_id=%s — %s",
                           round_number, report_id, e)
            continue
        payloads.append((doc, pdf_bytes))

    if not payloads:
        raise RuntimeError(
            f"No document of type {document_type} could be read: "
            + "; ".join(f"report_id={s['report_id']}: {s['reason']}"
                        for s in skipped))

    force_upload = sum(len(b) for _, b in payloads) > TOTAL_INLINE_LIMIT_BYTES

    count = len(payloads)

    # Never pluralise the type name itself — several of the eleven labels do not
    # take an "s" ("Speak up/Whistleblower", "GRI Index").
    # (n, of) when this type was too large for one call and was split.
    part_note = ""
    if part:
        part_note = (
            f" This is part {part[0]} of {part[1]} for this document type — "
            f"the company-year holds more {document_type}s than fit in one "
            f"call, so they are being read in batches.")

    if count > 1:
        given = (
            f"You are given {count} documents, all of type \"{document_type}\", "
            f"labelled below. Treat them as ONE combined body of evidence: a "
            f"disclosure missing from one may be present in another, and a "
            f"question should be scored 1 if ANY of them supports it. Several "
            f"documents of one type usually means the company split its "
            f"reporting — by jurisdiction, by division, or across volumes — so "
            f"read them as parts of a whole rather than as alternatives. "
            f"Attribute each quote to the document it came from by name in the "
            f"justification.")
    else:
        given = (
            f"You are given one document, of type \"{document_type}\", labelled "
            f"below. Attribute each quote to it by name in the justification.")

    # Whether anything else will be read after this round.
    #
    # This is not cosmetic. An earlier version told every round that "a null
    # simply moves on to the next document type" — which is FALSE on the last
    # round, and 83% of company-years have exactly one. Measured over 479
    # company-years whose input was byte-identical to the joint pass, that
    # phrasing cost 3.78 answered questions and 4.38 score points each: the
    # model was being pushed toward null a second time, on top of the "1 only
    # when explicitly and unequivocally met" rule the base prompt and the schema
    # already enforce, and told the cost was low when it was permanent.
    #
    # So the residual framing is stated ONLY when a later round actually exists,
    # and the final round gets the joint pass's own wording — because a final
    # round IS the joint pass, over whatever questions are still open.
    more_rounds_follow = round_number < total_rounds

    if round_number > 1:
        read_so_far = ("earlier documents of this same type, and this "
                       "company-year's other document types"
                       if part and part[0] > 1
                       else "this company-year's other document types")
        residual_note = (
            f"Earlier rounds have already read {read_so_far}. The catalogue "
            f"below contains ONLY the questions still unanswered — every "
            f"question omitted from it is already settled, so do not answer, "
            f"restate or speculate about anything that is not listed.\n\n")
    else:
        residual_note = ""

    if more_rounds_follow:
        closing = (
            "Answer from these documents alone. Other document types for this "
            "company-year are read separately, so a question this document set "
            "does not address stays open for them — leave it null rather than "
            "reaching for it here.")
    else:
        # Last (or only) round: nothing follows, so a null is the final answer.
        closing = (
            "This is the last document set for this company-year, so your "
            "answers here are final. Score a question 1 whenever these "
            "documents explicitly and unequivocally support it, and null "
            "otherwise.")

    contents: List[Any] = [
        f"REPORTING YEAR: {reporting_year}. This is curated data, not something "
        f"to verify — it is the financial year this reporting covers. Where a "
        f"document presents several years side by side, extract the figures for "
        f"this year.\n\n"
        f"This is round {round_number} of {total_rounds} for this company-year. "
        f"{given}{part_note}\n\n"
        f"{residual_note}"
        f"{closing}",
    ]

    if round_number > 1:
        contents.append(
            "Leave every `organisation` field null unless THESE documents state "
            "the value themselves. The organisation profile has already been "
            "taken from the primary filing; a figure re-derived from a policy "
            "document would be worse evidence for the same fact.")

    contents.append(
        "QUESTION CATALOGUE — answer every one of these in `answers`, using the "
        "exact question_id shown before the '::'.\n\n"
        + render_question_catalogue(only=residual))

    client = get_genai_client()
    uploaded = []
    attached: List[Dict[str, Any]] = []
    try:
        for index, (doc, pdf_bytes) in enumerate(payloads, start=1):
            report_id = doc.get("report_id")
            label = _document_label(doc, index)
            try:
                # Oversized PDFs go through the File API — inlining them is
                # rejected as prompt_block=BlockedReason.OTHER, which reads like
                # a content filter rather than a size limit.
                part, uploaded_file = make_pdf_part(
                    pdf_bytes, label, client=client, force_upload=force_upload)
            except Exception as e:
                skipped.append({"report_id": report_id,
                                "reason": f"could not attach: {e}"})
                logger.warning(
                    "[progressive] round %s: could not attach report_id=%s — %s",
                    round_number, report_id, e)
                continue

            if uploaded_file is not None:
                uploaded.append(uploaded_file)

            contents.append(f"--- {label} ---")
            contents.append(part)
            attached.append({
                "report_id": report_id,
                "document_type": doc.get("document_type"),
                "year": doc.get("year"),
                "document_url": doc.get("document_url"),
                "label": label,
                "size_bytes": len(pdf_bytes),
                "delivery": "file_api" if uploaded_file is not None else "inline",
            })

        if not attached:
            raise RuntimeError(
                f"No document of type {document_type} could be attached: "
                + "; ".join(f"report_id={s['report_id']}: {s['reason']}"
                            for s in skipped))

        try:
            wire = gemini_extract_from_gcs(
                EXTRACT_SECTION,
                prompt,
                None,              # PDFs are supplied as labelled extra parts
                schema_map=DIAL_GLOBAL_SCHEMA_MAP,   # -> DialGlobalExtractionWire
                model=EXTRACT_MODEL,
                extra_contents=contents,
                max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
                thinking=make_thinking_config(EXTRACT_MODEL),
            )
        except Exception as e:
            # Name the documents that were in the call. A prompt block is
            # usually one document's content rather than the set, and without
            # the manifest there is no way to tell which one to look at.
            manifest = "; ".join(
                f"report_id={a['report_id']} {a['size_bytes'] / 1e6:.1f}MB "
                f"via {a['delivery']}" for a in attached)
            raise RuntimeError(
                f"{e} | round {round_number} ({document_type}), "
                f"{len(residual)} questions asked, documents in call: "
                f"{manifest}") from e
    finally:
        # Uploads are only needed for the duration of the call.
        delete_uploaded_files(uploaded, client=client)

    return wire, attached, skipped


def _run_progressive_extraction_pass(
    documents: List[Dict[str, Any]],
    reporting_year: int,
) -> tuple:
    """
    Pass 1, progressive — Q1-Q79 assembled across one call per report type.

    Returns (extraction, prompt_meta, documents_used, trace). The first three
    match _run_extraction_pass() exactly, so the caller's scoring, storage,
    counters and logging are unchanged.

    Documents arrive already ordered by REN_ROUND_ORDER (see
    ren/document_selection.select_all_ren_documents), so grouping them by type in
    arrival order gives the round sequence directly: all annual reports in round
    1, all sustainability reports in round 2, and so on.

    `trace["rounds"]` is the audit trail: one entry per report type attempted,
    recording which documents were in the call, how many questions it was asked,
    which ones it filled, and the error if it failed. Without it there is no way
    to tell a question answered by the annual report from one answered by a
    two-page whistleblowing policy.

    A round that fails is recorded and skipped; the remaining types still run.
    That deliberately reverses the joint pass's all-or-nothing rule, which exists
    because a partial joint call is indistinguishable from a complete one. Here
    every round is in the payload, so a partial set is visible rather than
    silently flattering the score. If EVERY round fails there is no extraction
    and the company-year fails as it always did.
    """
    prompt, meta = get_instruction_with_meta(EXTRACT_SECTION)

    # One round per report type. `documents` is already in REN_ROUND_ORDER, and
    # OrderedDict preserves first-seen order, so the grouping IS the round order.
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for doc in documents:
        groups.setdefault(doc.get("document_type") or "Document", []).append(doc)

    # A type holding more documents than fit in one call is SPLIT across
    # consecutive rounds, not truncated.
    #
    # Truncating was the original behaviour and it quietly threw evidence away:
    # a DIAL Global company-year with 19 DEI reports had 13 of them dropped
    # before the model saw anything. The per-call limit is a context-window
    # constraint, not a judgement that the 7th document is worthless — so the
    # right response is another call, not a smaller set.
    #
    # Splitting is safe precisely because the merge is monotone. Part 2 of a
    # type is asked only what parts 1 and earlier types left open, exactly like
    # any other round, so the parts compose rather than compete.
    rounds_to_run: List[tuple] = []
    for document_type, group in groups.items():
        if len(group) <= MAX_DOCS_PER_ROUND:
            rounds_to_run.append((document_type, group, None))
            continue
        chunks = [group[i:i + MAX_DOCS_PER_ROUND]
                  for i in range(0, len(group), MAX_DOCS_PER_ROUND)]
        logger.info(
            "[progressive] year=%s: %s holds %s documents — splitting across "
            "%s rounds of at most %s", reporting_year, document_type,
            len(group), len(chunks), MAX_DOCS_PER_ROUND)
        for part, chunk in enumerate(chunks, start=1):
            rounds_to_run.append((document_type, chunk, (part, len(chunks))))

    if len(rounds_to_run) > MAX_ROUNDS_PER_YEAR:
        dropped = sum(len(c) for _, c, _ in rounds_to_run[MAX_ROUNDS_PER_YEAR:])
        logger.warning(
            "[progressive] year=%s needs %s rounds; capping at %s and dropping "
            "%s document(s) from the lowest-priority tail",
            reporting_year, len(rounds_to_run), MAX_ROUNDS_PER_YEAR, dropped)
        rounds_to_run = rounds_to_run[:MAX_ROUNDS_PER_YEAR]

    # Ordered, so the catalogue keeps schema order however many rounds run.
    residual: List[str] = list(QUESTION_CATALOGUE)
    answers: Dict[str, Dict[str, Any]] = {}
    provenance: Dict[str, Any] = {}
    organisation: Dict[str, Any] = {}
    organisation_provenance: Dict[str, Any] = {}

    documents_used: List[Dict[str, Any]] = []
    rounds: List[Dict[str, Any]] = []
    total_rounds = len(rounds_to_run)

    for round_number, (document_type, group, part) in enumerate(
            rounds_to_run, start=1):
        if not residual:
            logger.info(
                "[progressive] year=%s: all %s questions answered after %s "
                "round(s); %s report type(s) left unread",
                reporting_year, len(QUESTION_CATALOGUE), round_number - 1,
                total_rounds - round_number + 1)
            break

        started = time.time()
        record: Dict[str, Any] = {
            "round": round_number,
            "document_type": document_type,
            # (n, of) when a type was split across rounds, else None.
            "part": list(part) if part else None,
            "report_ids": [d.get("report_id") for d in group],
            "document_count": len(group),
            "year": reporting_year,
            "questions_asked": len(residual),
            "questions_filled": 0,
            "filled_question_ids": [],
            "skipped_documents": [],
            "error": None,
        }

        try:
            wire, attached, skipped = _run_extraction_round(
                prompt, group, round_number, total_rounds, document_type,
                reporting_year, residual, part)
        except Exception as e:
            # One unreadable report type must not cost the company-year the
            # evidence the others hold.
            record["error"] = str(e)
            record["duration_sec"] = round(time.time() - started, 2)
            rounds.append(record)
            logger.warning(
                "[progressive] year=%s round %s (%s) failed: %s",
                reporting_year, round_number, document_type, e)
            continue

        # Which documents actually evidenced this round's answers. With several
        # documents of one type in the call there is no way to attribute a single
        # answer to one of them — the model is told to name the source in the
        # justification, which is where that detail lives.
        round_report_ids = [a["report_id"] for a in attached]

        pending = set(residual)
        filled: List[str] = []
        for answer in wire.answers:
            question_id = (answer.question_id or "").strip()
            if question_id not in pending or question_id in answers:
                # Dropped for one of three reasons: the id was invented, an
                # earlier ROUND already settled it, or this same round already
                # emitted it. The `answers` check is what covers the third —
                # `pending` is a snapshot taken before the loop, so without it a
                # model that repeats itself inflates questions_filled and the
                # round's audit trail claims more than it moved.
                continue
            if answer.score is None or int(answer.score) != 1:
                # A null is "not in these documents", not "not disclosed" — the
                # question stays in the residual for the next round.
                continue
            answers[question_id] = {"score": 1,
                                    "justification": answer.justification}
            provenance[question_id] = {
                "round": round_number,
                "document_type": document_type,
                "report_ids": round_report_ids,
            }
            filled.append(question_id)

        # Q1-Q15: the first report type to state a value wins, so the primary
        # filing's figures are never overwritten by a later restatement.
        for field, value in wire.organisation.model_dump(mode="json").items():
            if value is None or organisation.get(field) is not None:
                continue
            organisation[field] = value
            organisation_provenance[field] = {
                "round": round_number,
                "document_type": document_type,
                "report_ids": round_report_ids,
            }

        residual = [q for q in residual if q not in answers]

        record.update({
            "questions_filled": len(filled),
            "filled_question_ids": filled,
            "questions_remaining": len(residual),
            "documents_in_call": round_report_ids,
            "skipped_documents": skipped,
            "bytes_in_call": sum(a["size_bytes"] for a in attached),
            "duration_sec": round(time.time() - started, 2),
        })
        rounds.append(record)

        for attachment in attached:
            documents_used.append({**attachment,
                                   "round": round_number,
                                   "round_questions_filled": len(filled)})

        logger.info(
            "[progressive] year=%s round %s/%s %s (%s doc(s)): asked %s, "
            "filled %s, %s left", reporting_year, round_number, total_rounds,
            document_type, len(attached), record["questions_asked"],
            len(filled), len(residual))

    if not documents_used:
        failures = "; ".join(
            f"round {r['round']} ({r['document_type']}, "
            f"report_ids={r['report_ids']}): {r['error']}"
            for r in rounds) or "no documents supplied"
        raise RuntimeError(
            f"Every progressive round failed for year={reporting_year} — "
            f"{failures}")

    wire = DialGlobalExtractionWire(
        organisation=OrganisationMetadataWire.model_validate(organisation),
        answers=[
            QuestionAnswer(question_id=question_id,
                           score=value["score"],
                           justification=value["justification"])
            for question_id, value in answers.items()
        ],
    )
    # assemble_extraction() fills every question the rounds never settled with a
    # null score, so the result still carries all 64 regardless of how many
    # rounds ran.
    extraction = assemble_extraction(wire)
    extraction = stamp_reporting_year(extraction, reporting_year)

    trace = {
        "mode": "progressive",
        # One round per report type — every document of a type is read together
        # in a single call, so a company filing three jurisdictional pay gap
        # reports spends one round on them, not three.
        "round_unit": "report_type",
        "rounds": rounds,
        "rounds_run": len(rounds),
        "rounds_failed": sum(1 for r in rounds if r["error"]),
        "report_types_available": total_rounds,
        "documents_available": len(documents),
        # Which document evidenced each question. This is the audit trail: it is
        # the only thing that distinguishes a question answered by the annual
        # report from one answered by a two-page whistleblowing policy.
        "answer_provenance": provenance,
        "organisation_provenance": organisation_provenance,
        "questions_answered": len(answers),
        "questions_total": len(QUESTION_CATALOGUE),
    }
    return extraction, meta, documents_used, trace


def _run_scoring_pass(
    extraction: DialGlobalExtractionSchema,
    reporting_year: int,
) -> tuple:
    """
    Pass 2 — CITIES scoring over the pass-1 JSON.

    Text-only: the scoring agent evaluates the extraction output, so the PDFs
    are not resent. The extraction JSON already carries the curated year in
    organisation.q02_year; finalise_scoring() stamps it onto the result too, so
    the stored year is the curated one whatever the model returns.
    """
    prompt, meta = get_instruction_with_meta(SCORE_SECTION)
    extraction_json = extraction.model_dump_json(indent=2)

    result = gemini_extract_from_gcs(
        SCORE_SECTION,
        prompt,
        None,                      # text-only: no document part
        schema=DialGlobalScoringSchema,
        model=SCORE_MODEL,
        extra_contents=[
            f"REPORTING YEAR: {reporting_year}\n\nEXTRACTION JSON:\n{extraction_json}"],
        max_output_tokens=SCORE_MAX_OUTPUT_TOKENS,
        thinking=make_thinking_config(SCORE_MODEL),
    )

    # The model supplies the rubric levels only. The arithmetic, the review
    # bookkeeping and the reporting year are all overwritten from the sources
    # of truth so the stored record never depends on the model getting them
    # right.
    result = finalise_scoring(result, reporting_year=reporting_year)
    return result, meta


def _consensus_level(levels: List[int]) -> int:
    """
    The agreed rubric level for one category across repeated scoring runs.

    A majority wins. With no majority — three runs, three different levels — the
    median is the least-wrong choice: it cannot land outside the observed range
    and it does not favour the first run, which is arbitrary.
    """
    counts = Counter(levels).most_common()
    if counts[0][1] > 1:
        return counts[0][0]
    return int(statistics.median(levels))


def _run_scoring_consensus(
    extraction: DialGlobalExtractionSchema,
    reporting_year: int,
    runs: int = SCORE_RUNS,
) -> tuple:
    """
    Pass 2, scored `runs` times, reduced to one result plus a disagreement flag.

    The model is a poor judge of its own uncertainty — asked directly it reports
    none, while scoring the same extraction repeatedly moves a category by a
    full rubric level. So uncertainty is measured instead of requested: score
    the identical input several times and treat disagreement between the runs as
    the signal. A category the runs agree on is one we can publish; a category
    they split on is one a human should re-read.

    The runs are independent and text-only, so they go out concurrently and cost
    roughly one run of wall time.

    Returns (scoring, meta, consensus) where `consensus` records what each run
    said, so a reviewer can see 4,4,5 rather than just "flagged".
    """
    if runs <= 1:
        scoring, meta = _run_scoring_pass(extraction, reporting_year)
        return scoring, meta, None

    with ThreadPoolExecutor(max_workers=runs) as pool:
        futures = [pool.submit(_run_scoring_pass, extraction, reporting_year)
                   for _ in range(runs)]
        # A single failed run is survivable; the consensus just narrows.
        outcomes, failures = [], []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as e:
                failures.append(e)

    if not outcomes:
        # Every run failed — surface the first error rather than an empty result.
        raise failures[0]

    scorings = [scoring for scoring, _ in outcomes]
    meta = outcomes[0][1]

    observed = {
        name: [getattr(s.categories, name).score for s in scorings]
        for name in CATEGORY_WEIGHTS
    }
    agreed = {name: _consensus_level(levels) for name, levels in observed.items()}

    # Publish the run that already matches the consensus most closely, so the
    # rationales belong to a coherent reading rather than being spliced from
    # runs that disagreed.
    base = max(scorings, key=lambda s: sum(
        getattr(s.categories, name).score == agreed[name] for name in agreed))

    split = []
    for name, levels in observed.items():
        category = getattr(base.categories, name)
        category.score = agreed[name]
        if len(set(levels)) > 1:
            # Disagreement is uncertainty, whatever the model claimed about its
            # own confidence. Never clear a flag the model raised itself.
            split.append(name)
            category.uncertainty_flagged = True
            category.score_rationale = (
                f"{category.score_rationale} "
                f"[Scored {runs}x; runs returned levels "
                f"{', '.join(str(level) for level in levels)} — published the "
                f"consensus level {agreed[name]}. Disagreement between runs on "
                f"identical input means this level is not reliable.]")

    # Levels changed, so the arithmetic and the curated year are redone.
    base = finalise_scoring(base, reporting_year=reporting_year)

    totals = sorted(s.overall_score.total_score_percentage for s in scorings)
    consensus = {
        "runs_requested": runs,
        "runs_completed": len(scorings),
        "runs_failed": len(failures),
        "observed_levels": observed,
        "split_categories": split,
        "totals": totals,
        "total_spread": round(totals[-1] - totals[0], 4),
    }
    return base, meta, consensus


def build_payload(
    extraction: DialGlobalExtractionSchema,
    scoring: Optional[DialGlobalScoringSchema],
    reporting_year: int,
    documents_used: List[Dict[str, Any]],
    scoring_consensus: Optional[Dict[str, Any]] = None,
    extraction_trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    One JSONB document per company-year, holding both passes.

    The extraction is worth keeping on its own — it is the evidence base behind
    the score — so it is stored alongside the scoring rather than being replaced
    by it. `scoring` is null on the unscored fallback path.

    `source_documents` records every report that fed this company-year, with
    document types and URLs. The bare ids also go into the row's
    source_report_ids column so provenance is queryable in SQL.

    `extraction_trace` is the progressive engine's round-by-round record — which
    document was read when, how many questions it was asked and which it filled.
    Null on the joint path, where there is only ever one round. It is spread into
    the payload rather than nested so `extraction_mode` can be read without
    knowing whether a trace exists.
    """
    payload = {
        "reporting_year": reporting_year,
        "source_documents": documents_used,
        "document_count": len(documents_used),
        "extraction": extraction.model_dump(mode="json"),
        "scoring": scoring.model_dump(mode="json") if scoring is not None else None,
        # What the repeated scoring runs each said. Null when scoring ran once.
        "scoring_consensus": scoring_consensus,
        "extraction_mode": "joint",
    }

    if extraction_trace:
        payload.update({
            "extraction_mode": extraction_trace.get("mode", "progressive"),
            "round_unit": extraction_trace.get("round_unit"),
            "extraction_rounds": extraction_trace.get("rounds") or [],
            "rounds_run": extraction_trace.get("rounds_run"),
            "rounds_failed": extraction_trace.get("rounds_failed"),
            "report_types_available": extraction_trace.get("report_types_available"),
            "documents_available": extraction_trace.get("documents_available"),
            "answer_provenance": extraction_trace.get("answer_provenance") or {},
            "organisation_provenance": extraction_trace.get(
                "organisation_provenance") or {},
            "questions_answered": extraction_trace.get("questions_answered"),
            "questions_total": extraction_trace.get("questions_total"),
        })

    return payload


def source_report_ids(documents: List[Dict[str, Any]]) -> List[int]:
    """Report ids that fed a company-year, for the source_report_ids column."""
    return sorted(
        int(d["report_id"]) for d in documents if d.get("report_id") is not None)


def run_company_year_extraction(
    *,
    task_id: Optional[str],
    company_id: int,
    uid: str,
    docs: List[Dict[str, Any]],
    store_fn: Callable[..., Optional[int]],
    label: str,
    section_name: str,
    year: Optional[int] = None,
    company_name: Optional[str] = None,
    progressive: bool = False,
) -> Dict[str, Any]:
    """
    Run the two-pass engine over every company-year of one company.

    Args:
        docs: already-selected documents, each
            {report_id, year, document_type, document_url}. Selection and
            sourcing are the caller's job — this function only groups by year.
        store_fn: called as
            store_fn(company_id=..., year=..., extracted_data=...,
                     uid=..., source_report_ids=[...], status=...)
            and returns an extraction id or None.
        label: pipeline label for the extraction log ("dial_global" / "ren").
        year: optional filter to a single reporting year.
        progressive: read the year's documents one call at a time, carrying the
            unanswered questions forward, instead of all together in one call.
            Off by default — DIAL Global and the original ReN task are
            unaffected. See _run_progressive_extraction_pass().

    Returns {company_id, results, errors, year}, the same shape as
    extract_env_gov_for_company.
    """
    started_at = time.time()

    counters = {
        "company_years": 0,
        "documents": 0,
        "extraction_ok": 0,
        "scoring_ok": 0,
        # Repeated scoring runs: how many completed, how many failed, and how
        # many company-years had the runs disagree on at least one category.
        "scoring_runs": 0,
        "scoring_run_failures": 0,
        "consensus_split": 0,
        "stored_scored": 0,
        "stored_unscored": 0,
        # Unscored writes DataX refused because a scored record already existed.
        "downgrade_suppressed": 0,
        # Documents excluded because the source data has no curated year.
        "missing_year": 0,
        "failed": 0,
        # Progressive mode only: model calls spent on pass 1, how many of those
        # rounds failed, and how many of the 64 questions ended up answered.
        # All stay 0 on the joint path.
        "extraction_rounds": 0,
        "extraction_round_failures": 0,
        "questions_answered": 0,
    }
    results: List[str] = []
    errors: List[str] = []
    # Prompt revisions actually used, so a bad batch is traceable to a prompt.
    prompt_versions: Dict[str, Any] = {}
    # Scores produced this run, for at-a-glance triage of the log record.
    scores: List[Dict[str, Any]] = []

    def _log_and_return():
        finished_at = time.time()
        save_extraction_log({
            "task_id": task_id,
            "section": section_name,
            "label": label,
            "company_id": company_id,
            "company_name": company_name,
            "uid": uid,
            "year": year,
            "started_at": int(started_at),
            "finished_at": int(finished_at),
            "duration_sec": finished_at - started_at,
            "models": {"extraction": EXTRACT_MODEL, "scoring": SCORE_MODEL},
            "prompts": prompt_versions,
            "counters": counters,
            "scores": scores,
            "errors_sample": errors[:20],
        })
        return {
            "company_id": company_id,
            "results": results,
            "errors": errors,
            "year": year,
        }

    if not docs:
        message = (f"No documents to extract for company_id={company_id} "
                   f"year={year} [{label}]")
        logger.info(message)
        errors.append(message)
        return _log_and_return()

    company_years, undated = group_docs_by_year(docs, year)

    for doc in undated:
        # Without a curated year there is no company-year to file the document
        # under, and guessing is exactly what this engine refuses to do.
        counters["missing_year"] += 1
        message = (
            f"No curated reporting year for report_id={doc.get('report_id')} "
            f"— excluded rather than inferring one")
        logger.warning("[%s] %s", label, message)
        errors.append(message)

    counters["company_years"] = len(company_years)

    for reporting_year, year_docs in company_years.items():
        # The per-call cap only binds the joint pass, where every document goes
        # into one context. Progressive reads one document per call, so its
        # bound is the round count (MAX_ROUNDS_PER_YEAR) rather than this.
        if not progressive and len(year_docs) > MAX_DOCS_PER_CALL:
            errors.append(
                f"company_id={company_id} year={reporting_year} has "
                f"{len(year_docs)} documents; using the first {MAX_DOCS_PER_CALL}")
            logger.warning(
                "[%s] Capping company_id=%s year=%s at %s of %s documents",
                label, company_id, reporting_year, MAX_DOCS_PER_CALL, len(year_docs))
            year_docs = year_docs[:MAX_DOCS_PER_CALL]

        try:
            # ---- Pass 1: extract Q1-Q79 from the year's documents ----
            extraction_trace = None
            if progressive:
                (extraction, extract_meta, documents_used,
                 extraction_trace) = _run_progressive_extraction_pass(
                    year_docs, reporting_year)
                counters["extraction_rounds"] += extraction_trace["rounds_run"]
                counters["extraction_round_failures"] += \
                    extraction_trace["rounds_failed"]
                counters["questions_answered"] += \
                    extraction_trace["questions_answered"]
                for record in extraction_trace["rounds"]:
                    if record["error"]:
                        errors.append(
                            f"Round {record['round']} "
                            f"(report_id={record['report_id']}, "
                            f"{record['document_type']}) failed for "
                            f"company_id={company_id} year={reporting_year}: "
                            f"{record['error']}")
            else:
                extraction, extract_meta, documents_used = _run_extraction_pass(
                    year_docs, reporting_year)
            prompt_versions[EXTRACT_SECTION] = extract_meta
            counters["extraction_ok"] += 1
            counters["documents"] += len(documents_used)

            # ---- Pass 2: score the extraction against the CITIES rubric ----
            scoring_consensus = None
            try:
                scoring, score_meta, scoring_consensus = _run_scoring_consensus(
                    extraction, reporting_year=reporting_year)
                prompt_versions[SCORE_SECTION] = score_meta
                counters["scoring_ok"] += 1
                if scoring_consensus:
                    counters["scoring_runs"] += scoring_consensus["runs_completed"]
                    counters["scoring_run_failures"] += scoring_consensus["runs_failed"]
                    if scoring_consensus["split_categories"]:
                        counters["consensus_split"] += 1
            except Exception as scoring_err:
                # Scoring is best-effort. A failure here must never cost us the
                # extraction, so fall through to the unscored store below.
                logger.error(
                    "[%s] Scoring pass failed for company_id=%s year=%s: %s",
                    label, company_id, reporting_year, scoring_err)
                errors.append(
                    f"Scoring pass failed for company_id={company_id} "
                    f"year={reporting_year}: {scoring_err}")
                scoring = None

            report_ids = source_report_ids(documents_used)
            status = STATUS_SCORED if scoring is not None else STATUS_UNSCORED
            record = store_fn(
                company_id=company_id,
                year=reporting_year,
                extracted_data=build_payload(
                    extraction, scoring, reporting_year, documents_used,
                    scoring_consensus=scoring_consensus,
                    extraction_trace=extraction_trace),
                uid=uid,
                source_report_ids=report_ids,
                status=status,
            )

            if not record and scoring is not None:
                # The scored write failed outright. Retry as unscored so the
                # extraction still lands — DataX refuses the write outright if
                # that would downgrade an existing scored record.
                errors.append(
                    f"Store failed for scored result, company_id={company_id} "
                    f"year={reporting_year} — retrying as unscored")
                record = store_fn(
                    company_id=company_id,
                    year=reporting_year,
                    extracted_data=build_payload(
                        extraction, None, reporting_year, documents_used,
                        extraction_trace=extraction_trace),
                    uid=uid,
                    source_report_ids=report_ids,
                    status=STATUS_UNSCORED,
                )
                status = STATUS_UNSCORED

            if not record:
                counters["failed"] += 1
                errors.append(
                    f"Store failed for company_id={company_id} "
                    f"year={reporting_year} (status={status})")
                continue

            extraction_id = record.get("extraction_id")
            doc_count = len(documents_used)

            if record.get("downgrade_suppressed"):
                # We sent unscored; DataX kept the scored record it already had.
                # Report what is actually stored, not what we tried to write.
                counters["downgrade_suppressed"] += 1
                results.append(
                    f"Extracted {reporting_year} from {doc_count} document(s), "
                    f"but kept the existing SCORED record "
                    f"(extraction_id={extraction_id})")
                continue

            if status == STATUS_SCORED:
                counters["stored_scored"] += 1
                total = scoring.overall_score.total_score_percentage
                # Derived, not stored: the scoring schema carries the flag per
                # category, so the run-level view is assembled here.
                flagged = [name for name in CATEGORY_WEIGHTS
                           if getattr(scoring.categories, name).uncertainty_flagged]
                scores.append({
                    "year": reporting_year,
                    "document_count": doc_count,
                    "total_score_percentage": total,
                    "human_review_required": bool(flagged),
                    "flagged_categories": flagged,
                    "questions_answered": (
                        extraction_trace["questions_answered"]
                        if extraction_trace else None),
                })
                detail = (
                    f" over {extraction_trace['rounds_run']} round(s), "
                    f"{extraction_trace['questions_answered']}/"
                    f"{extraction_trace['questions_total']} questions answered"
                    if extraction_trace else "")
                results.append(
                    f"Extracted + scored {reporting_year} from {doc_count} document(s)"
                    f"{detail} (score={total}%, extraction_id={extraction_id})")
            else:
                counters["stored_unscored"] += 1
                results.append(
                    f"Extracted {reporting_year} from {doc_count} document(s), "
                    f"stored UNSCORED (extraction_id={extraction_id})")

        except Exception as e:
            counters["failed"] += 1
            logger.exception(
                "[%s] Extraction failed for company_id=%s year=%s",
                label, company_id, reporting_year)
            errors.append(
                f"Extraction error for company_id={company_id} year={reporting_year}: {e}")

    return _log_and_return()
