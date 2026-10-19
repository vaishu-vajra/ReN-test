# Financial extraction — codebase walkthrough

For the team building the financial extraction pipeline. Read the
[README](../README.md) first to get the project running. This file covers
where your work goes, which existing code to learn from, what you may change
and what you must not touch.

The requirements document is the source of truth for *what* to build. This
file covers *where* and *how* in this codebase.

---

## 1. The shape of the work

You are building one new package, `data_ingestion_engine/financial/`. It
contains a copy of the existing extraction engine, adapted to financial
filings, plus the queue, triggers and endpoints around it. Nearly all of your
code lives inside that package. Section 5 lists the few small, agreed edits
outside it.

Suggested layout. Adjust it if you have a better one, but keep everything
under `financial/`:

```
data_ingestion_engine/financial/
  __init__.py
  engine.py        the copied, adapted extraction engine
  selection.py     which filings feed a company's fiscal year; fiscal year resolution
  normalize.py     validation and normalization of extracted values
  store.py         the store function that writes to the financial tables
  prompts.py       prompt resolution, with this package's own fallback copies
  queue.py         the single entry point every trigger calls
  tasks.py         Celery tasks, on their own queue
  views.py         on-demand API and the internal trigger endpoint
  auth.py          inbound service-to-service authentication
  tests/
```

---

## 2. The existing engine: what to copy, what to leave behind

The engine is [`tasks/company_year_engine.py`](../data_ingestion_engine/tasks/company_year_engine.py).
It was built for an earlier one-off project and is **frozen**. Copy from it;
never import from it or edit it.

**Copy and adapt:**

| Function | What it does | Notes for your copy |
|---|---|---|
| `load_pdf_bytes`, `fit_pdf_for_gemini` | Fetch a PDF and shrink it under Gemini's size limit | Generic; copy as is. |
| `group_docs_by_year`, `curated_year` | Group a company's documents by reporting year | Replace the year logic with your fiscal-year resolution. |
| `_document_label` | Labels each PDF so the model can tell documents apart | Generic. |
| `_run_extraction_pass` | **Joint mode**: all of a year's documents in one model call | Bind your schema and prompt instead of the hard-coded ones. |
| `_run_extraction_round`, `_run_progressive_extraction_pass` | **Progressive mode**: one call per document type, carrying unanswered fields forward | "Unanswered" is defined against the old schema's questions. Redefine it against your schema. |
| `source_report_ids` | Provenance: which documents fed a record | Generic. |
| `run_company_year_extraction` | Orchestrates a company: groups by year, extracts, stores, writes the log | Keep the structure. Replace the counters (see below). |

**Leave behind:**

- `_run_scoring_pass`, `_consensus_level`, `_run_scoring_consensus` and every
  `SCORE_*` constant. That scoring pass belongs to the earlier project, and
  financial extraction has no scoring.
- `build_payload`. It assembles the earlier project's payload, including its
  scores. Write your own.
- The imports from `schemas/dial_global_schemas.py` and `utils/dial_global/`.
- The log counters tied to the old schema: `scoring_*`, `stored_scored`,
  `stored_unscored`, `downgrade_suppressed`, `questions_answered` (a fixed
  count of 64). Replace them with "fields filled vs expected" for your
  schema.
- The `DIAL_GLOBAL_*` environment variables. Use a `FINANCIAL_` prefix for
  your own settings, for example `FINANCIAL_EXTRACT_MODEL`.

---

## 3. Reference implementations — read these, copy the patterns

| File | Pattern to copy |
|---|---|
| [`tasks/dial_global/extract.py`](../data_ingestion_engine/tasks/dial_global/extract.py) | How a pipeline wires documents → engine → store inside a Celery task: `bind=True`, `rate_limit`, explicit `soft_time_limit`/`time_limit`, a dedicated queue. |
| [`utils/dial_global/store_data.py`](../data_ingestion_engine/utils/dial_global/store_data.py) → `store_dial_global_extraction` | The store contract against DataX: upsert on (company, year), both 200 and 201 count as success, refusing to downgrade a complete record to a less complete one, and source document ids kept **without** a foreign key so deleting a document never cascade-deletes the record. Read the docstring in full. |
| [`api/views/dial_global/extract_progressive.py`](../data_ingestion_engine/api/views/dial_global/extract_progressive.py) | The on-demand endpoint conventions: input validation, `dry_run`, a `confirm` token above `CONFIRM_THRESHOLD`, chunked fan-out with a Celery `group`. |
| [`utils/ren/document_selection.py`](../data_ingestion_engine/utils/ren/document_selection.py) | How document selection and ordering rules are written and documented: one constant per document type, `canonical_report_type`, and comments saying *why* each rule exists. |
| [`scripts/run_local_pdf.py`](../data_ingestion_engine/scripts/run_local_pdf.py) | Runs the engine over local PDFs with **writes off by default**. Build the same for financial extraction. It is the fastest development loop. |

---

## 4. Shared infrastructure you can call, but not change

Use these as they are. If one needs to change, raise it with us first:
other pipelines in production depend on them.

| Module | Use it for |
|---|---|
| `utils/geminiProcessing.py` | `get_genai_client()` (carries the request timeout — never build a bare client), `make_pdf_part()` (inline vs. upload by size), `delete_uploaded_files()`, `make_config_for(schema=...)`, `make_thinking_config()` |
| `utils/store_data.py` | `_session()` and `_headers(uid)` for DataX calls, `DATAX_INTERNAL`, `fetch_reports_by_id`, `fetch_company` |
| `utils/gc_service.py` | `save_extraction_log()`, GCS reads |
| `utils/compress_pdf.py`, `utils/download_file.py` | PDF compression and download |
| `api/auth.py` | `FirebaseAuthentication` for user-facing endpoints |
| `internal_jwt.py` | Minting the JWT this engine sends to DataX |

---

## 5. Agreed changes outside your package

These are the only edits allowed outside `financial/`. Keep each one minimal.

1. **Surface the new document's id at ingestion.**
   [`utils/store_data.py`](../data_ingestion_engine/utils/store_data.py) →
   `store_report` returns `(company_id, error)` and discards the DataX
   response body, so nothing downstream knows *which* document was just
   created. The manual-ingestion trigger needs that id. Make it available in
   a backward-compatible way. Its only caller is `utils/process_download.py`.
   **Agree the exact change with us before you start — we may make it
   ourselves.**
2. **Manual ingestion hook.**
   [`utils/process_download.py`](../data_ingestion_engine/utils/process_download.py) →
   `process_download`, directly after `store_ingesting_report` succeeds. Add
   one call to your queue. Wrap it so a failure in your code **never** fails
   the ingestion: log it and let reconciliation pick the document up later.
   Both manual paths, spreadsheet ingestion (`/download-xls-urls/`) and
   failed-link re-ingestion (`/reingest-failed-urls/`), pass through this
   function, so one hook covers both. The earlier project's ingestion uses its
   own copy in `utils/dial_global/process_download.py`; do not hook that one.
3. **Routes.** Register your endpoints in
   [`urls.py`](../data_ingestion_engine/urls.py).
4. **A Celery queue.** Add a `financial` queue to the `-Q` list in
   `k8s/base/celery-deployment.yaml`, `docker-compose.yml` and the README.
   All three, or tasks are silently never run.
5. **Configuration.** Non-secret settings go in the `configMapGenerator` of
   `k8s/overlays/{dev,prod}/kustomization.yaml`. Secrets go through us; they
   live in Secret Manager. Add every new key to `.env.example`.
6. **Dependencies.** Add test tooling (pytest and friends) to `requirements.txt`.

### Frozen — do not modify

```
data_ingestion_engine/tasks/company_year_engine.py
data_ingestion_engine/tasks/dial_global/
data_ingestion_engine/tasks/ren/
data_ingestion_engine/utils/dial_global/
data_ingestion_engine/utils/ren/
data_ingestion_engine/api/views/dial_global/
data_ingestion_engine/api/views/ren/
data_ingestion_engine/schemas/dial_global_schemas.py
data_ingestion_engine/utils/getInstruction.py
```

Check a branch before you open a PR. This must print nothing:

```bash
git diff --name-only origin/dev...HEAD | grep -E \
  '^data_ingestion_engine/(tasks/(company_year_engine\.py|dial_global/|ren/)|utils/(dial_global/|ren/|getInstruction\.py)|api/views/(dial_global|ren)/|schemas/dial_global_schemas\.py)'
```

---

## 6. The two triggers, in this codebase

**Manual ingestion** happens in this repository (section 5, item 2).

**Scheduled ingestion** does not. The daily Cloud Run jobs live in the
`datax-backend` repository and write documents to the database directly, so
nothing here sees them arrive. You build an internal endpoint they call at the
end of each run with the ids they stored. Two things to know:

- **There is no inbound service-to-service authentication yet.**
  `internal_jwt.py` only *mints* tokens for outbound calls to DataX, and
  `FirebaseAuthentication` verifies end users, not services. Build an
  authenticator in `financial/auth.py` that verifies an HS256 JWT. Agree the
  audience and issuer values with us — they must differ from the ones this
  engine uses towards DataX, so a token for one direction cannot be replayed
  in the other.
- **Do not trust the trigger to be delivered.** Cross-service calls fail
  silently. Reconciliation (see the requirements) is what guarantees nothing
  is missed. Design it first, not last.

---

## 7. Traps we have already fallen into

| Trap | What happens | What to do |
|---|---|---|
| Unknown prompt name falls through to `"default_instruction"` | `utils/getInstruction.py` returns the literal string `"default_instruction"` for any section it does not know. If Langfuse is unreachable and your prompt has no fallback, the model is sent that string as its prompt. **No error is raised and garbage gets stored.** | Keep your own fallback copies in `financial/prompts.py`. Resolve Langfuse yourself the way `get_instruction_with_meta` does, and **raise** if neither source returns your prompt. Do not add your prompts to `getInstruction.py`. |
| Langfuse label = `ENVIRONMENT` | Prompts are fetched under the label `dev` or `production`, taken from `ENVIRONMENT`. A prompt published only to `dev` does not exist in prod. | Publish to both labels. Log the version you resolved on every run. |
| Year `1900` | `utils/store_data.py` stores an unknown document year as `1900`. | Treat 1900 as "unknown" in fiscal-year resolution — never as a real year. |
| One company, two ids | The same company can exist under two ids in DataX. | Resolve by ISIN; see `scripts/resolve_company_cohort.py`. |
| Thinking tokens eat the output budget | Gemini's reasoning tokens count against `max_output_tokens`, so a large schema comes back truncated mid-JSON. | Cap thinking with `make_thinking_config()`, size output budgets for your schema, and treat a truncated response as a failure, never as a partial success. |
| Large PDFs | Gemini rejects files over its limit. | `fit_pdf_for_gemini` and `make_pdf_part` already handle this. Use them. |
| Long filings exceed the prompt-token limit | `media_resolution` is not set anywhere, so Gemini tokenizes every PDF page at full resolution. A 1,155-page document set measured well over the 1,048,576-token limit (`400 INVALID_ARGUMENT`), and the same set at `MEDIA_RESOLUTION_LOW` came to 307,235 tokens. A 10-K or 20-F with its exhibits is exactly this kind of document. | Set it for your package's calls only, and choose LOW or MEDIUM by comparing extraction quality on known-good filings first. Lower resolution can lose small print in tables. Do not change the shared client: other pipelines depend on its current behaviour. |
| Celery `-Q` | A queue missing from a worker's `-Q` list is never consumed, and nothing errors. | Section 5, item 4. |
| Celery default time limit | The project default soft limit is 800 s (`celery.py`), too short for a multi-round company. | Set explicit `soft_time_limit`/`time_limit` on every task, as `tasks/dial_global/extract.py` does. |
| Silent stalls on bad networks | A broken IPv6 route makes each outbound call hang for minutes. | See README → Troubleshooting. |

---

## 8. Working agreement

- **Branches.** Branch from `dev` and open PRs into `dev`. A merge to `dev`
  deploys to the dev environment; `main` is production and is ours to promote.
- **PR size.** One requirements item per PR where you can. A reviewer has to
  be able to hold the whole change in their head.
- **Every PR includes:** tests for what it adds, the frozen-paths check from
  section 5 coming back empty, and docs updated if behaviour changed.
- **No live model calls in tests.** Record real responses once and replay
  them.
- **Comments say why, not what.** The existing code documents its decisions
  heavily, for example `utils/ren/document_selection.py` and
  `tasks/company_year_engine.py`. Match that. The next person to change your
  code needs your reasons, not a description of the syntax.
- **Questions** about intent, data or the agreed changes come to us.
  Don't guess.
