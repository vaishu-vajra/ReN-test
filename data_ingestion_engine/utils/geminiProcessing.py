import io
import json
import logging
import os
import random
import time
from google import genai
from google.genai import errors
from google.genai.types import Part
from google.genai.types import GenerateContentConfig
import httpx
import threading
from google.genai import types
from pydantic import ValidationError

from data_ingestion_engine.utils.gc_service import download_blob_bytes_from_url
from data_ingestion_engine.utils.getInstruction import get_instruction
from data_ingestion_engine.schemas.section_schemas import SCHEMA_MAP
from data_ingestion_engine.utils.store_data import patch_report


def _resolve_schema(section_name: str, schema=None, schema_map=None):
    """
    Pick the Pydantic class that constrains the model's response.

    Precedence: an explicit `schema` class wins; otherwise look `section_name`
    up in `schema_map`; otherwise fall back to the env/gov SCHEMA_MAP, which is
    what every pre-existing caller relies on.
    """
    if schema is not None:
        return schema
    return (schema_map or SCHEMA_MAP)[section_name]


def make_config_for(section_name: str, schema=None, schema_map=None,
                    max_output_tokens: int = None, thinking=None):
    schema = _resolve_schema(section_name, schema, schema_map)
    print(f"Using schema for section {section_name}: {schema}")
    return GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=max_output_tokens,
        thinking_config=thinking,
    )


# Thinking tokens are drawn from max_output_tokens, not from a separate
# allowance. Left uncapped, a hard extraction spends the whole budget thinking
# and the JSON is truncated mid-string: one company-year came back with
# thoughts=31454, candidates=1300 against a 32768 ceiling. Capping thinking
# keeps room for the answer.
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "8192"))
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low")


def make_thinking_config(model: str):
    """
    The right thinking knob for `model`, or None if it takes neither.

    Gemini 3.x replaced 2.5's numeric `thinking_budget` with a coarse
    `thinking_level`; sending the wrong one is a 400, so pick by model family
    rather than setting both. Only the 2.x family is matched explicitly: models
    after 3.8 reject `thinking_budget` outright instead of remapping it.
    """
    if not model:
        return None
    if model.startswith("gemini-2"):
        return types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET)
    return types.ThinkingConfig(thinking_level=GEMINI_THINKING_LEVEL)


# Every Gemini call in the engine runs on this unless the caller overrides it.
# Env-overridable so a model can be rolled back without a deploy.
MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# Documented Gemini limits, and the headroom we keep under each:
#   inline (base64) payload   100 MB total per request, PDFs capped at 50 MB
#   Files API                 2 GB per file, 20 GB per project, 48h retention
#
# A single PDF above PDF_INLINE_LIMIT_BYTES goes through the Files API, and so
# does the whole set when the combined payload would approach the request cap —
# two large documents inline returned "503 The request timed out" rather than a
# clean error, so it is worth staying well clear.
PDF_INLINE_LIMIT_BYTES = int(
    os.getenv("GEMINI_PDF_INLINE_LIMIT_BYTES", str(40 * 1024 * 1024)))
TOTAL_INLINE_LIMIT_BYTES = int(
    os.getenv("GEMINI_TOTAL_INLINE_LIMIT_BYTES", str(60 * 1024 * 1024)))

# Back-compat alias for the per-document threshold.
GEMINI_INLINE_LIMIT_BYTES = PDF_INLINE_LIMIT_BYTES

# How long to wait for an uploaded file to become ACTIVE before giving up.
_FILE_ACTIVE_TIMEOUT = float(os.getenv("GEMINI_FILE_ACTIVE_TIMEOUT", "120"))


# Hard ceiling on a single Gemini request, in milliseconds.
#
# Without this the SDK waits on the socket forever. A dropped connection that
# is never signalled — the remote closes, the socket sits in CLOSE_WAIT, no
# exception is raised — stalls the whole run rather than failing one document:
# a 20-company batch hung here for 28 minutes at 0% CPU with three CLOSE_WAIT
# sockets to Google before it was killed. A timeout turns that into an ordinary
# error, which the existing retry/backoff then handles.
#
# Sized from 445 measured calls across every run to date:
#
#   median 5.5s | p90 32s | p99 118s | slowest single call 232s (3.9 min)
#   calls over 5 minutes: 0
#
# 10 minutes is ~2.6x the slowest call ever observed, so it cannot cut short
# legitimate work, and it keeps a hung company inside the Celery soft_time_limit
# (1800s): a company runs its company-years sequentially, so even if BOTH hang
# the task spends 20 minutes and still returns its errors instead of being
# killed mid-flight with the successful year's work lost.
#
# Note this is a hard failure, not a retry — the loop below only retries
# errors.APIError, and a socket timeout surfaces as httpx.ReadTimeout. That is
# deliberate: a timeout has already spent its full budget, so retrying it five
# times would blow the soft limit that this value exists to respect.
GEMINI_REQUEST_TIMEOUT_MS = int(
    os.getenv("GEMINI_REQUEST_TIMEOUT_MS", str(10 * 60 * 1000)))


def get_genai_client():
    """A client bound to the configured API key.

    The SDK falls back to GOOGLE_API_KEY when GEMINI_API_KEY is unset, so
    passing the latter through as None is intentional.
    """
    return genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options=types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS),
    )


def make_pdf_part(pdf_bytes: bytes, display_name: str, client=None,
                  force_upload: bool = False):
    """
    A content Part for one PDF, uploading it first when it cannot be inlined.

    `force_upload` is set by the caller when the combined payload of a request
    is too large to inline, even though this document on its own would fit.

    Returns (part, uploaded_file_or_None). The caller owns the returned file and
    should pass it to delete_uploaded_files() when the request is done —
    otherwise it sits in the project's Files API storage for 48 hours and counts
    against the 20 GB project quota.
    """
    if not force_upload and len(pdf_bytes) < PDF_INLINE_LIMIT_BYTES:
        return types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), None

    client = client or get_genai_client()
    logging.info("Uploading %s (%.1f MB) via the File API — too large to inline",
                 display_name, len(pdf_bytes) / 1e6)
    uploaded = client.files.upload(
        file=io.BytesIO(pdf_bytes),
        config=types.UploadFileConfig(mime_type="application/pdf",
                                      display_name=display_name[:120]),
    )

    # A freshly uploaded file is PROCESSING for a moment; referencing it before
    # it is ACTIVE fails the request.
    deadline = time.time() + _FILE_ACTIVE_TIMEOUT
    while str(getattr(uploaded.state, "name", uploaded.state)) == "PROCESSING":
        if time.time() > deadline:
            raise RuntimeError(
                f"Uploaded file {display_name} was still PROCESSING after "
                f"{_FILE_ACTIVE_TIMEOUT:.0f}s")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    state = str(getattr(uploaded.state, "name", uploaded.state))
    if state != "ACTIVE":
        raise RuntimeError(
            f"Uploaded file {display_name} is {state}, not ACTIVE")

    return types.Part.from_uri(file_uri=uploaded.uri,
                               mime_type="application/pdf"), uploaded


def delete_uploaded_files(uploaded, client=None) -> None:
    """Best-effort cleanup of File API uploads. Never raises."""
    if not uploaded:
        return
    client = client or get_genai_client()
    for f in uploaded:
        try:
            client.files.delete(name=f.name)
        except Exception as e:
            logging.warning("Could not delete uploaded file %s: %s",
                            getattr(f, "name", f), e)


def make_config_for_extract(max_output_tokens: int = 8192, thinking=None):
    return GenerateContentConfig(
        response_mime_type="text/plain",
        max_output_tokens=max_output_tokens,
        thinking_config=thinking,
    )


# Sections that produce large multi-table Markdown reports need a bigger budget
# AND must run on the full Flash model — the lite model silently truncates long outputs
_LARGE_OUTPUT_SECTIONS = {"gap_analysis", "priorities_and_sentiment_analysis", "data_points","summary", "earnings_delta"}
_LARGE_OUTPUT_MAX_TOKENS = 16384

# Sections that reason over SEVERAL documents at once need more than the
# single-document ceiling: the earnings delta renders a nine-section report with
# three comparison tables over up to five transcripts, and thinking tokens are
# drawn from the same budget. At 16384 the model ran out mid-report.
_MULTI_DOC_MAX_TOKENS = int(os.getenv("GEMINI_MULTI_DOC_MAX_TOKENS", "32768"))
_LITE_MODEL = os.getenv("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
_FULL_MODEL = MODEL_ID


# ---- Minimal retry knobs (env-overridable) ----
_GEMINI_MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "5"))
_GEMINI_BASE_BACKOFF = float(
    os.getenv("GEMINI_BASE_BACKOFF", "1.0"))  # seconds
_GEMINI_MAX_BACKOFF = float(os.getenv("GEMINI_MAX_BACKOFF", "20.0"))  # seconds


def _sleep_backoff(attempt: int):
    # exponential backoff with jitter
    delay = min(_GEMINI_MAX_BACKOFF, _GEMINI_BASE_BACKOFF *
                (2 ** attempt)) + random.random()
    time.sleep(delay)


def gemini_extract_from_gcs(
    section: str,
    prompt: str,
    gcs_uri: str,
    *,
    schema=None,
    schema_map=None,
    model: str = None,
    pdf_bytes: bytes = None,
    extra_contents: list = None,
    max_output_tokens: int = None,
    thinking=None,
):
    """
    One schema-constrained Gemini call over a PDF, with retry/backoff on
    429/5xx and a `parsed` -> raw-JSON -> `model_validate` fallback path.

    Called positionally as gemini_extract_from_gcs(section, prompt, gcs_uri) it
    behaves exactly as before: schema from SCHEMA_MAP, model MODEL_ID, PDF
    downloaded from `gcs_uri`. The keyword-only arguments are what let the DIAL
    Global engine reuse it instead of forking it:

      schema         Pydantic class to constrain the response with, bypassing
                     SCHEMA_MAP entirely.
      schema_map     Alternative section -> class map (e.g. DIAL_GLOBAL_SCHEMA_MAP).
      model          Override the Gemini model for this call.
      pdf_bytes      Already-downloaded PDF, so a caller that hits the same
                     document twice downloads it once. Pass gcs_uri=None
                     together with this.
      extra_contents Additional parts appended after the prompt — used to hand a
                     second pass the first pass's JSON.
      thinking       ThinkingConfig capping how much of max_output_tokens the
                     model may spend thinking. Left None it is derived from the
                     model — see make_thinking_config — because letting the
                     model decide truncates the JSON mid-string.

    With gcs_uri=None and pdf_bytes=None the call is text-only: prompt plus
    extra_contents, no document part. That is how the DIAL Global scoring pass
    runs, since it reasons over the pass-1 JSON rather than the PDF.
    """
    GENAI_CLIENT = get_genai_client()
    model = model or MODEL_ID
    if thinking is None:
        thinking = make_thinking_config(model)
    config = make_config_for(section, schema=schema, schema_map=schema_map,
                             max_output_tokens=max_output_tokens,
                             thinking=thinking)

    if pdf_bytes is None and gcs_uri is not None:
        pdf_bytes = download_blob_bytes_from_url(gcs_uri)

    contents = [prompt]
    if pdf_bytes:
        contents.append(types.Part.from_bytes(
            data=pdf_bytes, mime_type="application/pdf"))
    if extra_contents:
        contents.extend(extra_contents)

    response = None
    for attempt in range(_GEMINI_MAX_ATTEMPTS):
        try:
            response = GENAI_CLIENT.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except errors.APIError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < _GEMINI_MAX_ATTEMPTS - 1:
                _sleep_backoff(attempt)
                continue
            raise

    # Try structured parse first
    parsed = getattr(response, "parsed", None)

    if parsed is None:
        raw_text = _extract_json_text(response)
        if not raw_text:
            # No text part at all. Almost always MAX_TOKENS (the response was
            # truncated before any JSON was emitted — note that 2.5's thinking
            # tokens draw on the same budget) or a safety block. Report which,
            # rather than letting json.loads(None) raise a TypeError that says
            # nothing about the cause.
            raise RuntimeError(
                f"Gemini returned no text for section '{section}' "
                f"({_response_diagnosis(response)})")
        print(f"Raw extracted text for section {section}:", raw_text)
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as je:
            # Truncated output is the usual cause — the response was cut mid-JSON,
            # so attach finish_reason and the token counts rather than leaving
            # "Unterminated string at char N" to be interpreted on its own.
            logging.warning(
                "Model returned non-JSON text (cannot parse): %s", je)
            raise ValueError(
                f"{je} | {_response_diagnosis(response)} "
                f"| {len(raw_text)} chars received") from je

        schema_cls = _resolve_schema(section, schema, schema_map)
        try:
            parsed = schema_cls.model_validate(data)  # Pydantic v2
        except ValidationError as ve:
            logging.error("Schema validation failed: %s", ve)
            raise

    # Optional: post-normalization (dimension focus, year coercion already handled by validators)
    return parsed


def stream_gemini(section: str, file_uri: str):
    """
    Generator that streams JSON responses from Gemini for the given section and PDF URI.
    """
    GENAI_CLIENT = get_genai_client()
    # Sections with large Markdown output: use the full model + higher token budget
    # The lite model silently truncates long table outputs mid-stream
    is_large = section in _LARGE_OUTPUT_SECTIONS
    model = _FULL_MODEL if is_large else _LITE_MODEL
    max_tokens = _LARGE_OUTPUT_MAX_TOKENS if is_large else 8192
    # Cap thinking: it spends max_output_tokens, so an uncapped budget truncates
    # these Markdown reports the same way it truncated DIAL Global's JSON.
    config = make_config_for_extract(max_output_tokens=max_tokens,
                                     thinking=make_thinking_config(model))
    prompt = get_instruction(section)

    try:
        # Retrieve and encode the PDF byte
        doc_data = httpx.get(file_uri).content

        # --- ONLY CHANGE: retry opening the stream on 429/5xx ---
        for attempt in range(_GEMINI_MAX_ATTEMPTS):
            try:
                response_stream = GENAI_CLIENT.models.generate_content_stream(
                    model=model,
                    contents=[
                        types.Part.from_bytes(
                            data=doc_data,
                            mime_type='application/pdf',
                        ),
                        prompt
                    ],
                    config=config,
                )
                break  # success
            except errors.APIError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < _GEMINI_MAX_ATTEMPTS - 1:
                    _sleep_backoff(attempt)
                    continue
                # keep original behavior for non-retryables or after last attempt
                yield {"error": f"Gemini APIError {e.code}: {e.message}"}
                return
        # --------------------------------------------------------

        print("Response stream created successfully.")
        truncated = False
        for chunk in response_stream:
            truncated = _hit_token_ceiling(chunk) or truncated
            text = getattr(chunk, "text", None)
            if text:
                yield text
        if truncated:
            logging.warning(
                "Gemini hit max_output_tokens (%d) for section '%s' — response truncated",
                max_tokens, section)
            yield _TRUNCATION_SENTINEL

    except errors.APIError as api_err:
        # yield a JSON error object then stop (unchanged behavior)
        yield {"error": f"Gemini APIError {api_err.code}: {api_err.message}"}


def stream_gemini_multi(section: str, documents: list):
    """
    Generator that streams a response from Gemini for a section that reasons over
    SEVERAL PDFs at once (e.g. the 4-quarter earnings delta analysis).

    `documents` is an ordered list (oldest -> newest) of dicts:
        {"label": "Doc 1: Q3 FY25", "url": "<pdf url>"}
    Each PDF is preceded by a text part carrying its label so the model can produce
    the required `[Doc X: Period, Page Y]` citations.
    """
    # get_genai_client() rather than a bare genai.Client: the shared factory is
    # what carries GEMINI_REQUEST_TIMEOUT_MS, so a hand-rolled client here waits
    # on a dead socket forever.
    GENAI_CLIENT = get_genai_client()
    is_large = section in _LARGE_OUTPUT_SECTIONS
    model = _FULL_MODEL if is_large else _LITE_MODEL
    max_tokens = _MULTI_DOC_MAX_TOKENS if is_large else 8192
    # Cap thinking. Left unset, Gemini 3 defaults to its high thinking level and
    # spends the whole of max_output_tokens deliberating — on a window whose
    # labels disagreed with the transcripts it burned the entire budget arguing
    # with itself, and the truncated scratchpad was what got streamed and saved.
    config = make_config_for_extract(max_output_tokens=max_tokens,
                                     thinking=make_thinking_config(model))
    prompt = get_instruction(section)

    try:
        contents = []
        for doc in documents:
            doc_data = httpx.get(doc["url"], follow_redirects=True, timeout=120).content
            contents.append(f"--- {doc['label']} ---")
            contents.append(
                types.Part.from_bytes(
                    data=doc_data,
                    mime_type='application/pdf',
                )
            )
        contents.append(prompt)

        for attempt in range(_GEMINI_MAX_ATTEMPTS):
            try:
                response_stream = GENAI_CLIENT.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=config,
                )
                break  # success
            except errors.APIError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < _GEMINI_MAX_ATTEMPTS - 1:
                    _sleep_backoff(attempt)
                    continue
                yield {"error": f"Gemini APIError {e.code}: {e.message}"}
                return

        print(f"Response stream created successfully for {len(documents)} documents.")
        truncated = False
        for chunk in response_stream:
            truncated = _hit_token_ceiling(chunk) or truncated
            text = getattr(chunk, "text", None)
            if text:
                yield text
        if truncated:
            logging.warning(
                "Gemini hit max_output_tokens (%d) for section '%s' over %d documents "
                "— response truncated", max_tokens, section, len(documents))
            yield _TRUNCATION_SENTINEL

    except errors.APIError as api_err:
        yield {"error": f"Gemini APIError {api_err.code}: {api_err.message}"}


def stream_gemini_explain(context: dict, widget_data: dict):
    """
    Streams the explanation for a specific widget using Gemini.
    Constructs the prompt by replacing placeholders with context and widget data.
    """
    GENAI_CLIENT = get_genai_client()

    # Use the extract config (text/plain output)
    config = make_config_for_extract(thinking=make_thinking_config(MODEL_ID))
    instruction = get_instruction("explain")

    # Replace placeholders
    prompt = instruction.replace("[COMPANY_NAME]", context.get("company_name", "Unknown Company")) \
                        .replace("[TICKER_SYMBOL]", context.get("ticker_symbol", "N/A")) \
                        .replace("[WIDGET_TITLE]", context.get("widget_title", "Widget")) \
                        .replace("[WIDGET_JSON_PAYLOAD]", json.dumps(widget_data, indent=2))

    try:
        # Retry logic similar to stream_gemini
        for attempt in range(_GEMINI_MAX_ATTEMPTS):
            try:
                # Note: No PDF part here, just text prompt
                response_stream = GENAI_CLIENT.models.generate_content_stream(
                    model=MODEL_ID,
                    contents=[prompt],
                    config=config,
                )
                break  # success
            except errors.APIError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < _GEMINI_MAX_ATTEMPTS - 1:
                    _sleep_backoff(attempt)
                    continue
                yield {"error": f"Gemini APIError {e.code}: {e.message}"}
                return

        for chunk in response_stream:
            text = getattr(chunk, "text", None)
            if text:
                yield text

    except errors.APIError as api_err:
        yield {"error": f"Gemini APIError {api_err.code}: {api_err.message}"}



# Yielded by the streaming generators when the model hit max_output_tokens, so
# collect_and_patch can tell a complete response from a cut-off one. It never
# reaches the client — collect_and_patch swallows it.
_TRUNCATION_KEY = "__truncated__"
_TRUNCATION_SENTINEL = {_TRUNCATION_KEY: True}


def _hit_token_ceiling(chunk) -> bool:
    """True when a streamed chunk closes the candidate with finish_reason MAX_TOKENS."""
    for candidate in getattr(chunk, "candidates", []) or []:
        reason = getattr(candidate, "finish_reason", None)
        if str(getattr(reason, "name", reason)) == "MAX_TOKENS":
            return True
    return False


def _looks_like_output(text: str) -> bool:
    """
    Whether `text` even resembles what a section is meant to produce — a Markdown
    report or a JSON document.

    This exists because `chunk.text` is not the safeguard it looks like. The SDK
    drops a part only when `part.thought` is exactly True; a part that carries the
    model's reasoning with the flag absent is concatenated into `.text` like any
    answer. That is how a transcript of the model talking to itself ended up saved
    as an earnings delta report. A truncated response that starts as neither
    Markdown nor JSON is that scratchpad, not a short report.
    """
    stripped = text.lstrip()
    if stripped.startswith(("#", "{", "[", "|")):
        return True
    head = stripped[:2000]
    return any(line.startswith("#") for line in head.splitlines())


def _is_error_payload(text: str) -> bool:
    """
    Returns True if `text` is a JSON object whose only key is 'error'.
    This matches chunks like: {"error": "Gemini APIError 503: ..."}
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(stripped)
        return isinstance(data, dict) and list(data.keys()) == ["error"]
    except json.JSONDecodeError:
        return False


def collect_and_patch(gen, report_id: str, section: str, uid: str):
    """
    Wraps a generator `gen` that yields text chunks.
    Accumulates them and, after iteration, calls patch_report() via INTERNAL auth (uid).
    Skips the DB write if the entire response is an error payload so that transient
    API errors (e.g. 503) never overwrite a previously-saved good value, and skips it
    again when the model was cut off at max_output_tokens before it produced anything
    recognisable as a report — that output is the model's own reasoning, and storing
    it replaces a good value with a transcript of the model thinking out loud.
    """
    aggregated = ""
    truncated = False
    try:
        for chunk in gen:
            # An out-of-band signal from the generator, not part of the response
            if isinstance(chunk, dict) and chunk.get(_TRUNCATION_KEY):
                truncated = True
                continue
            # if chunk is already a dict/error, stringify it
            if not isinstance(chunk, str):
                chunk = json.dumps(chunk)
            aggregated += chunk
            yield chunk
    finally:
        # once the generator is exhausted (or on error), store the result in the background
        def run_patch():
            if _is_error_payload(aggregated):
                print(
                    f"[Info] Skipping patch for report {report_id} section '{section}' "
                    f"— response was an error payload: {aggregated[:120]}"
                )
                return
            if truncated and not _looks_like_output(aggregated):
                print(
                    f"[Warning] Skipping patch for report {report_id} section '{section}' "
                    f"— hit max_output_tokens before producing a report; the "
                    f"{len(aggregated)} chars received are model reasoning, not output: "
                    f"{aggregated[:120]!r}"
                )
                return
            try:
                success = patch_report(report_id, aggregated, section, uid)
                if not success:
                    print(f"[Warning] Failed to patch report {report_id}")
            except Exception as e:
                print(f"[Error] Exception while patching report {report_id}: {e}")

        threading.Thread(target=run_patch, daemon=True).start()


def _extract_json_text(resp):
    """
    The response text, or None when the response carries no text part.

    Returns None rather than raising so the caller can attach a diagnosis —
    a truncated or blocked response is a normal outcome worth reporting well,
    not an unexpected error.
    """
    try:
        text = resp.text  # some SDK versions expose .text
        if text:
            return text
    except Exception:
        pass
    for c in getattr(resp, "candidates", []) or []:
        content = getattr(c, "content", None)
        for p in (getattr(content, "parts", None) or []):
            if getattr(p, "text", None):
                return p.text
    return None


def _response_diagnosis(resp) -> str:
    """Why a response carried no usable text — finish reason, safety, usage."""
    bits = []
    for c in getattr(resp, "candidates", []) or []:
        reason = getattr(c, "finish_reason", None)
        if reason is not None:
            bits.append(f"finish_reason={getattr(reason, 'name', reason)}")
        if getattr(c, "safety_ratings", None):
            blocked = [f"{getattr(r.category, 'name', r.category)}"
                       for r in c.safety_ratings if getattr(r, "blocked", False)]
            if blocked:
                bits.append("blocked=" + ",".join(blocked))
    feedback = getattr(resp, "prompt_feedback", None)
    if getattr(feedback, "block_reason", None):
        bits.append(f"prompt_block={feedback.block_reason}")
    usage = getattr(resp, "usage_metadata", None)
    if usage is not None:
        bits.append(
            f"tokens: prompt={getattr(usage, 'prompt_token_count', '?')} "
            f"candidates={getattr(usage, 'candidates_token_count', '?')} "
            f"thoughts={getattr(usage, 'thoughts_token_count', '?')}")
    return "; ".join(bits) or "no diagnostic detail on the response"
