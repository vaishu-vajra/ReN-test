# data-ingestion-engine

The backend that turns company disclosure documents into data. It acquires
filings and reports, normalizes them to PDF, stores them, and runs LLM passes
over them to produce:

- **Analyses** — per-document written reports (summary, risk, gap, priorities
  and sentiment, period-over-period earnings delta), streamed to the web app
  and saved against the document. See `api/views/gemini.py`.
- **Extractions** — structured JSON records per company per reporting year,
  assembled from several documents.

It owns no database. Companies, documents and results live in the **DataX**
backend (a separate service) and this engine reads and writes them over DataX's
internal HTTP API.

> Working on the financial extraction pipeline? Read
> [docs/financial-extraction-walkthrough.md](docs/financial-extraction-walkthrough.md)
> after this file.

---

## How it fits together

```
                       ┌──────────────────────── this repository ─────────────────────────┐
 data team ─ spreadsheet ─▶ API (Django/DRF) ──▶ Celery workers ──▶ download, convert, store
 web app ── analysis ─────▶ API ──▶ Gemini ──▶ streamed back + saved                  │
                       └──────────────┬──────────────────────────────┬─────────────────┘
                                      │ internal API (JWT)           │ PDFs
                                      ▼                              ▼
                               DataX backend                 Google Cloud Storage
                     (companies, documents, results)
```

| Piece | Where |
|---|---|
| HTTP API | `data_ingestion_engine/api/views/`, routes in `data_ingestion_engine/urls.py` |
| Background work | Celery tasks in `data_ingestion_engine/tasks/`, Redis as broker |
| LLM calls | `utils/geminiProcessing.py` (Google Gemini, PDF in, JSON or Markdown out) |
| Prompts | Langfuse, with in-repo fallbacks in `utils/getInstruction.py` |
| DataX client | `utils/store_data.py`, authenticated by `internal_jwt.py` |
| Document storage | `utils/gc_service.py` (Google Cloud Storage) |
| Inbound auth | `api/auth.py` — every endpoint needs a Firebase ID token |
| Deployment | `Dockerfile`, `cloudbuild.yaml`, `k8s/` (GKE, kustomize overlays per env) |

### Repository map

```
data_ingestion_engine/
  api/views/        HTTP endpoints, one module per feature
  tasks/            Celery tasks
    company_year_engine.py   company-year extraction engine (DIAL Global / ReN — frozen)
    dial_global/, ren/       pipelines built on that engine (frozen)
    downloads.py             document ingestion tasks
  utils/            shared helpers: Gemini, DataX client, GCS, prompts, citations
  schemas/          Pydantic response schemas for the LLM
  scripts/          operator scripts, run with `python -m data_ingestion_engine.scripts.<name>`
k8s/
  base/, overlays/  manifests, and per-environment overlays for dev and prod
  cluster/          one-off cluster patches, applied by hand
docs/
  financial-extraction-walkthrough.md   onboarding for the financial extraction work
  runbooks/         how to run specific batch operations
visa/               visa application evaluation — a separate project, not used by the engine
internal_jwt.py     mints the service-to-service JWT for DataX
```

---

## Local setup

### 1. Prerequisites

- **Python 3.10** — matches the production image. Newer versions work locally,
  but test against 3.10 before you merge.
- **Redis** — Celery's broker.
  - macOS: `brew install redis && brew services start redis`
  - Debian/Ubuntu: `sudo apt-get install redis-server && sudo systemctl start redis-server`
- **Ghostscript** and **LibreOffice** — used to compress PDFs and convert
  office documents during ingestion. Only needed if you run ingestion locally.
  - macOS: `brew install ghostscript && brew install --cask libreoffice`
  - Debian/Ubuntu: `sudo apt-get install ghostscript libreoffice-writer libreoffice-calc libreoffice-impress`
- **gcloud CLI**, logged in for Application Default Credentials (used for
  Google Cloud Storage):
  ```bash
  gcloud auth application-default login
  ```

### 2. Install

```bash
git clone <repository-url>
cd data-ingestion-engine
python3.10 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium       # headless browser used to fetch HTML documents
```

### 3. Configure

```bash
cp .env.example .env
```

Fill in the values — we give you dev credentials at onboarding. The app
**refuses to start** without these three:

| Variable | Why it is required |
|---|---|
| `DATAX_API_URL` | Every read and write goes to DataX. Checked at import. |
| `INTERNAL_JWT_SECRET` | Signs the service-to-service JWT for DataX. Checked at import. |
| `FIREBASE_CREDS` | Service-account JSON used to verify inbound tokens. Checked at import. |

The rest are needed only by the features that use them. `.env.example` groups
them and says which feature each one is for. Never commit `.env` — it is
gitignored.

### 4. Run

Two processes, in two terminals:

```bash
# API
PYTHONUNBUFFERED=1 python manage.py runserver 0.0.0.0:8000

# Workers — list every queue (see the warning below)
celery -A data_ingestion_engine worker --loglevel=info --concurrency=2 -Q default,dial_global,ren
```

Or run everything in containers:

```bash
docker compose up --build
```

> **Always pass `-Q` with every queue.** A worker started without it consumes
> only the `default` queue. Tasks routed to any other queue are accepted and
> then sit in Redis forever, with no error anywhere. If you add a queue, add it
> here, in `docker-compose.yml` and in `k8s/base/celery-deployment.yaml`.

`python manage.py migrate` only creates Django's own bookkeeping tables in a
local SQLite file. No application data lives here; skipping it is harmless.

### 5. Call an endpoint

Every endpoint expects a Firebase ID token for a dev user. We give you a test
account; exchange its credentials for a token with Firebase's REST API:

```bash
curl -s "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<FIREBASE_WEB_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"email":"<test-user>","password":"<password>","returnSecureToken":true}' | jq -r .idToken
```

Then:

```bash
curl -X POST http://localhost:8000/risk-analysis/ \
  -H "Authorization: Bearer <id-token>" \
  -H "Content-Type: application/json" \
  -d '{"report_id": <a dev report id>}'
```

Tokens expire after an hour. `GET /health/` needs no token and is the quickest
check that the server is up.

---

## Checks

The repository has no unit-test suite yet. Offline verification scripts:

```bash
python -m data_ingestion_engine.scripts.verify_citations   # citation link pipeline, no model calls
```

New work should add real tests (pytest) — see the walkthrough.

---

## Deployment

Cloud Build (`cloudbuild.yaml`) builds the `web` and `celery` images, fetches
the environment's `.env` from Secret Manager, and applies the matching overlay
with `kubectl apply -k k8s/overlays/<env>`.

| Branch | Environment | Namespace |
|---|---|---|
| `dev` | dev | `dev` |
| `main` | prod | `prod` |

- **Secrets** (API keys, credentials) live in Secret Manager and reach pods as
  the `app-secrets` Secret. `k8s/overlays/<env>/.env.example` lists the keys.
- **Non-secret settings** are `configMapGenerator` literals in
  `k8s/overlays/<env>/kustomization.yaml`, reviewable in git.

Production runs 6 Celery pods at `--concurrency=4`, so 24 company tasks run in
parallel. See `k8s/SCALING_RUNBOOK.md`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every outbound call takes minutes, then times out | A broken IPv6 route on your network. Python tries each IPv6 address in turn before falling back to IPv4; `curl` races both and hides it. Test with `curl -6`. Fix: `sudo networksetup -setv6off Wi-Fi` (macOS). |
| `print()` output never appears under gunicorn | stdout is block-buffered. Set `PYTHONUNBUFFERED=1`. |
| An analysis endpoint drops with `WORKER TIMEOUT` | Gunicorn's `--timeout 120` is shorter than a long Gemini generation. Use `--timeout 600` locally. |
| A task is accepted but never runs | The worker is not listening on that task's queue. Check `-Q`. |
| Gemini returns truncated JSON | Thinking tokens count against `max_output_tokens`. See the comments in `utils/geminiProcessing.py`. |
| A prompt change has no effect | Prompts come from Langfuse first. The in-repo copy is used only when Langfuse cannot be reached, or when the section is listed in `LOCAL_ONLY_SECTIONS` in `utils/getInstruction.py`. |
