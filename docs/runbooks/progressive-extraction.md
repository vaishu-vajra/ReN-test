# Progressive ReN extraction — runbook

Re-runs 797 flagged ReN company-years through a gap-filling engine: **11 report
types** instead of 4, **every** document of each type, and **one model call per
report type** with the still-unanswered questions carried forward.

Cohort: `data_ingestion_engine/Problem Companies Full List - Sheet2.csv`,
rows scoring ≤70 → **797 company-years / 567 companies**, baseline mean 41.4%,
every one of them currently scored on exactly **one** document.

---

## The mechanism in one paragraph

**One round per report type**, ordered broadest-first (annual → sustainability →
DEI → … → speak up). Every annual report goes into round 1 *together*; every
gender pay gap report into its own round together; and so on. Round 1 is asked
all 64 questions. Whatever it answers **1** is settled and *removed from the
catalogue*, so round 2 is shown only what is still open, round 3 only what rounds
1–2 left, until the types run out or all 64 are answered. A `null` never settles
anything — it means "not in these documents", so the question rolls forward. The
merge is therefore **monotone: a later round can only add an answer, never
retract one.**

The **category, not the document, is the unit** — a company filing UK, Ireland
and France gender pay gap reports has published one body of evidence in three
files, and a question answered by combining them is a real answer. Provenance is
recorded per question as `{round, document_type, report_ids}`; which specific
file a quote came from is named in the justification.

So seven documents across four types cost **four rounds, not seven**.

---

## Step 0 — Snapshot the baseline. **Do this first.**

Storage upserts on `(ren_company_id, year)`. The run **replaces** each existing
scored row in place and DataX keeps no history. This file is the only rollback.

```bash
python -m data_ingestion_engine.scripts.snapshot_ren_baseline \
    --uid <uid> --api-url https://<prod-datax-host> \
    --out ren_baseline_$(date +%F).json
```

Confirm `rows_found` ≈ 797 before continuing. Anything under `missing_pairs`
has no baseline and cannot be rolled back.

## Step 1 — Audit the classifier. **Gate — no model calls.**

```bash
python -m data_ingestion_engine.scripts.audit_ren_document_types \
    --uid <uid> --api-url https://<prod-datax-host> \
    --out-csv ren_type_audit.csv --out-json ren_type_audit.json
```

Read the **`unclassified`** section. It is the only evidence the 11 patterns hold
against prod values rather than the 161-row ingestion mastersheet they were
written from. Then use the output to set:

- `REN_MAX_DOCS_PER_YEAR` ← from `max_documents_in_one_year`
- the run's cost ← from `estimated_model_calls`

If it warns that **no row carries `document_title`**, the title fallback is inert
against that endpoint and documents typed `Others` will stay unread.

## Step 2 — Dry run

```bash
curl -X POST https://ingestion-engine.myrenx.ai/ren/extract/progressive/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

## Step 3 — Pilot ten company-years. **Gate.**

Pick a spread from the audit's distribution — at least one 1-document year
(nothing should change) and one 10+ document year.

```bash
curl -X POST .../ren/extract/progressive/ -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pairs": [[25,2024],[58,2026], ...]}'
```

Then export and read the round traces before committing:

```bash
python -m data_ingestion_engine.scripts.export_ren_progressive \
    --uid <uid> --api-url https://<prod-datax-host> \
    --baseline ren_baseline_<date>.json --out pilot.json
```

**Expect scores to rise**, and know why: a union of positives across documents is
more permissive than a single joint read. Progressive rows are *not*
apples-to-apples with the unconverted rest of the leaderboard. That is a
publishing decision to make explicitly — the same family as the open question
of whether company-years scored on a single document should be published.

## Step 4 — Full run

Patch the HPA first; it scales on CPU at 70% and this workload sits at ~24%, so
it never scales itself, and `minReplicas` resets to 3 on redeploy.

```bash
kubectl -n prod scale deploy/celery --replicas=<n>
curl -X POST .../ren/extract/progressive/ -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"batch_size": 20}'
```

## Step 5 — The deliverable

```bash
python -m data_ingestion_engine.scripts.export_ren_progressive \
    --uid <uid> --api-url https://<prod-datax-host> \
    --baseline ren_baseline_<date>.json \
    --out ren_progressive_$(date +%F).json
```

Run it inside a celery pod and `kubectl cp` it out — the leaderboard endpoint
times out at this row count (gunicorn 120s). Add `--no-extraction` for a
shareable file without the 64-question bodies.

---

## Rollback

```bash
# what would change — no writes
python -m data_ingestion_engine.scripts.restore_ren_baseline \
    --uid <uid> --api-url https://<prod-datax-host> \
    --baseline ren_baseline_<date>.json

# actually roll back
... --apply

# or just part of it
... --pairs 25:2024,58:2026 --apply
```

Dry run by default. Only rows that **actually changed** are restored — a row
still matching the snapshot is reported `unchanged` and skipped — so it is safe
after a partial run and safe to run twice.

Two things it cannot do:

- A row that did not exist when the snapshot was taken cannot be restored to
  absent; DataX has no delete on this path. Reported as `missing_now` and
  re-created instead.
- DataX refuses to let an `unscored` record overwrite a `scored` one, so an
  unscored baseline over a scored current row comes back
  `downgrade_suppressed` rather than restoring.

It also warns if the snapshot's backend differs from the one being restored to —
replaying a prod snapshot into dev would write one environment's scores over
another's.

---

## Configuration

| variable | default | what it does |
|---|---|---|
| `REN_MAX_DOCS_PER_TYPE` | `0` (unlimited) | documents of one type per year. `1` restores one-per-type |
| `REN_MAX_DOCS_PER_YEAR` | `20` | hard cap per company-year; drops the lowest-priority tail |
| `DIAL_GLOBAL_MAX_DOCS_PER_ROUND` | `6` | documents in **one** call, i.e. one type's worth |
| `DIAL_GLOBAL_MAX_ROUNDS_PER_YEAR` | `20` | round ceiling; can only bind at 11 (the type count) |
| `REN_PROGRESSIVE_SOFT_TIME_LIMIT` | `5400` | Celery soft limit; rounds are sequential |

`DIAL_GLOBAL_MAX_DOCS_PER_ROUND` is the one that matters. Grouping by category
re-introduces the multi-PDF context pressure that a per-document design avoided,
so it is what stops nine annual reports mis-filed under one year from becoming a
900-page call.

## Cost and latency

Each PDF is still read exactly once, so **total input tokens barely move** — the
only overhead is the residual catalogue (~2–4k tokens) repeated per extra round.
What changes is wall clock: rounds are sequential, so a company-year with four
report types is ~4× the elapsed time of one joint call.

Grouping by category rather than by document caps the sequential chain at **11
rounds** however many documents a year holds, and it is usually far fewer — the
audit's `documents_per_company_year` distribution tells you the real number.

The compensation is that **no single call carries the whole document set any
more**, so the `1,048,576` prompt-token failures — Gemini tokenises every PDF page at
full resolution because `media_resolution` is unset — largely disappear
without having to decide LOW vs MEDIUM on it.
That protection is weaker than it was in the per-document design, though: a round
carrying six large annual reports can still approach the limit, which is what
`DIAL_GLOBAL_MAX_DOCS_PER_ROUND` guards.

## Reading the output

`export_ren_progressive.py` prints `yield_by_report_type` — questions answered
per round, per type. A type running many rounds for near-zero yield is one to
drop from `REN_ROUND_ORDER`; that table is the evidence for whether widening
from four types to eleven actually paid.

## Before the full run

Check overlap with `failed_ren_2026-08-19.csv` (133 stale pairs). They are a
separate backlog and neither run should clobber the other.

## Verification

```bash
python -m data_ingestion_engine.scripts.verify_progressive_engine
```

65 offline checks: classification against real stored values, selection and cap
behaviour, and residual monotonicity — including that a second document cannot
overwrite what the first found.
