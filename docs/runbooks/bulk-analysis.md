# Bulk risk / gap / earnings-delta run

Two scripts. The first is free and decides *what* runs; the second spends the
money. Never skip the first.

```
data_ingestion_engine/scripts/build_analysis_cohort.py   GETs only, no cost
data_ingestion_engine/scripts/run_bulk_analysis.py       the model calls
data_ingestion_engine/utils/bulk_analysis.py             classification + windows
```

## In scope

Documents classified as **Earnings Call Transcript, 10-K, 10-Q, 20-F, Annual
Report**, in the last 2 reporting years.

| Analysis | Unit | Saved on |
|---|---|---|
| `risk_analysis` | one document | that document |
| `gap_analysis` | one document | that document |
| `earnings_delta` | rolling window: each transcript + up to 4 preceding transcripts | the newest transcript in the window |

Any of these that already holds analysis is skipped, not recomputed.

Delta baselines may be older than the 2-year window — a Q1 2025 delta with no
2024 history to compare against is worthless — which is why the cohort is built
with `--history-years 1` of extra reading.

## 1. Build the cohort

> **DataX has no S&P 500.** The internal API exposes `/companies/ids` and
> `/companies/{id}`; nothing there carries index membership. `--exchange` gives
> you *US-listed companies in DataX*, a superset of the index. For the real
> constituents pass `--tickers` with a list and the cohort is intersected
> against it; symbols that match nothing are reported.

**Preferred: export the list from the DataX database.** The universe scan costs
one GET per company purely because the exchange lives on `/companies/{id}`. A
CSV exported from the DB skips it entirely, and can carry columns the internal
API never returns. Needs a `company_id` column; `company_name`, `symbol`,
`exchange`, `country`, `sector` are carried through to the audit CSV if present.

```bash
python -m data_ingestion_engine.scripts.build_analysis_cohort \
    --uid <uid> --companies-csv datax_sp500.csv \
    --out sp500_cohort.json --out-csv sp500_cohort.csv
```

`--exchange`, `--country` and `--tickers` all apply on top, so one export of
"all US-listed" can be narrowed to the index in the same run.

**Duplicate company rows are collapsed automatically.** DataX holds the same
company under two canonical ids — AT&T is 169 and 253, Atlassian 260 and 2879;
83 tickers in the NYSE/NASDAQ export do this. The builder keeps the id that
holds documents (the same test `resolve_company_cohort.py` applies) and records
the rest under `dropped_duplicates` in the cohort JSON. When BOTH ids hold
documents it keeps the larger id and prints the pair — check those by hand, the
tool cannot tell which is canonical. `--keep-duplicates` disables this, which
means paying twice for one company.

### Scan instead (no export available)

```bash
python -m data_ingestion_engine.scripts.build_analysis_cohort \
    --uid <uid> --exchange NYSE,NASDAQ \
    --out sp500_cohort.json --out-csv sp500_cohort.csv
```

Prints the document mix and **the exact number of model calls the run implies**.
Read the CSV before going further. Companies with zero in-scope documents are
counted separately — a large number there means the filter is wrong, not that
the corpus is empty.

One GET per company (the exchange only exists on `/companies/{id}`), so the
universe scan takes a few minutes. Re-use the JSON rather than rebuilding it.

## 2. Plan the run — free

```bash
python -m data_ingestion_engine.scripts.run_bulk_analysis \
    --uid <uid> --cohort sp500_cohort.json --plan
```

## 3. A small real run, results kept local

```bash
python -m data_ingestion_engine.scripts.run_bulk_analysis \
    --uid <uid> --cohort sp500_cohort.json \
    --api-url https://datax-backend-dev-....run.app --limit 20
```

Costs model calls. Writes nothing to DataX. Inspect
`bulk_analysis_results.json` before scaling up.

## 4. The real thing

```bash
python -m data_ingestion_engine.scripts.run_bulk_analysis \
    --uid <uid> --cohort sp500_cohort.json --store --out-csv results.csv
```

`--store` fills **empty** sections. It does not replace analysis that already
exists, and this repo's `.env` points at **prod**. The script names the host and
the number of reports affected and asks for confirmation (`--yes` skips it).

### Existing analysis is never replaced

A section that already holds a value is checked **twice**:

1. **Before the model call**, so no money is spent on work that would be thrown
   away. A `--coverage` export answers this for free where it can.
2. **Again against a fresh read, immediately before the write.** A model call
   takes minutes; the first check is stale by then. If something else populated
   the section meanwhile — another shard of this job, a user in the product —
   the write is abandoned and the generated text is kept in the results file
   instead.

The coverage export is trusted in **one direction only**. "Already populated" is
believed; "not populated" is not, because a stale or incomplete export would
otherwise licence an overwrite. A coverage miss always falls through to a live
read.

Replacing existing analysis requires `--overwrite`, which is never on by
default and must not be added to the CronJob.

Other rails, all on by default:

- error payloads, empty responses, and output truncated before the model wrote
  anything real are **never stored** — same guards `collect_and_patch` uses, so
  a transient 503 can't overwrite good analysis
- every finished unit is appended to `<out>.state.jsonl`; re-running resumes and
  does not re-burn spend

## Running the whole corpus

The cohort does not have to be an index. Dropping `--tickers` and keeping
`--exchange NYSE,NASDAQ --country US` gives every US-listed company DataX holds
(~4,500 after duplicates collapse), and the same run works unchanged. Two things
change at that scale.

### 1. Export the coverage map, or the skip check dominates

Skipping already-populated sections is the default, but without an export it
costs a request per report to find out. Over a full corpus that is tens of
thousands of GETs before the first model call. Export the answer instead:

```sql
-- one row per (report_id, section) that ALREADY has analysis
SELECT report_id, 'risk_analysis' AS section FROM company_documents
 WHERE risk_analysis IS NOT NULL AND btrim(risk_analysis) <> ''
UNION ALL
SELECT report_id, 'gap_analysis' FROM company_documents
 WHERE gap_analysis IS NOT NULL AND btrim(gap_analysis) <> ''
UNION ALL
SELECT report_id, 'earnings_delta' FROM company_documents
 WHERE earnings_delta IS NOT NULL AND btrim(earnings_delta) <> '';
```

(Table and column names are whatever the DataX schema actually calls them —
confirm before running.) A wide shape works too: `report_id` plus one
boolean-ish column per section.

```bash
python -m data_ingestion_engine.scripts.run_bulk_analysis \
    --uid <uid> --cohort cohort.json --coverage datax_coverage.csv --store
```

The file is authoritative: a report absent from it is treated as having no
analysis, and no request is made to confirm. Re-export it between runs. Without
`--coverage` the check still happens, one read per report rather than per unit,
and the run prints how many reads it needed.

### 2. Throughput is the real limit, not correctness

Order of magnitude: a full US cohort is tens of thousands of model calls, each
of which downloads PDFs and streams a long response — call it 60-120s. At the
default 4 workers that is *weeks* of wall clock. Options, in order of
preference:

- raise `--workers` until you hit the Gemini per-minute quota, not past it
- split the corpus and run the chunks as separate processes, each with its own
  `--state` file: `--company-ids` takes an explicit list, and the state files
  keep them from colliding
- let it resume. The state file means an interrupted run costs nothing but the
  in-flight calls, so running it in stretches is fine

Get the real number from step 1 before planning any of this.

## Running it nightly on GKE

The Cloud Run Jobs + Cloud Scheduler pattern maps onto this cluster as a
**CronJob** — same idea, and it can reuse the celery image, the `app-secrets`
generated from `.env`, and the `ingestion-engine-ksa` Workload Identity binding
that already gives pods access to Gemini. Manifests are in
[`k8s/base/bulk-analysis.yaml`](k8s/base/bulk-analysis.yaml).

The scheduler is the easy half. The half that matters is stopping at dawn
without losing work:

| Layer | What it does |
|---|---|
| `schedule: "0 22 * * *"` + `timeZone` | starts the run at 22:00 wall clock |
| `--stop-at=06:00` | the script stops *starting* new calls at 06:00, lets in-flight ones finish, exits 0 |
| `activeDeadlineSeconds: 30600` | hard backstop 30 min later, if the script ever hangs |
| `terminationGracePeriodSeconds: 600` | SIGTERM drains instead of SIGKILLing a call mid-write |
| `concurrencyPolicy: Forbid` | last night's run overrunning never collides with tonight's |
| state file on a PVC | tomorrow resumes exactly where today stopped |

A stopped run exits **0**, not non-zero: ending at the window boundary is the
design, and a failure code there would report a broken Job every morning.

### First-time setup

The CronJob ships **suspended** and there is no `BULK_ANALYSIS_UID` set, so
deploying it changes nothing until you do both of these.

```bash
# 1. deploy (Cloud Build does this on push; manually:)
kubectl apply -k k8s/overlays/prod

# 2. put the cohort and coverage files on the volume
kubectl scale deploy/bulk-analysis-shell -n prod --replicas=1
POD=$(kubectl get pod -n prod -l app=bulk-analysis-shell -o name | head -1)
kubectl cp corpus_cohort.json  prod/${POD#pod/}:/state/cohort.json
kubectl cp datax_coverage.csv  prod/${POD#pod/}:/state/coverage.csv

# 3. dry-run inside the cluster, using the real secrets and network
kubectl exec -n prod ${POD#pod/} -- python -m \
    data_ingestion_engine.scripts.run_bulk_analysis \
    --uid <uid> --cohort /state/cohort.json --coverage /state/coverage.csv \
    --state /state/run.state.jsonl --plan

# 4. release the volume — it is ReadWriteOnce, and a shell pod holding it can
#    leave the nightly Job unschedulable
kubectl scale deploy/bulk-analysis-shell -n prod --replicas=0

# 5. set BULK_ANALYSIS_UID in k8s/overlays/prod/kustomization.yaml, re-apply,
#    then start it
kubectl patch cronjob bulk-analysis -n prod -p '{"spec":{"suspend":false}}'
```

### Day to day

```bash
kubectl get cronjob bulk-analysis -n prod                  # is it on, when did it last run
kubectl get jobs -n prod -l job-name                       # last 7 nights
kubectl logs -n prod job/<job-name> --tail=50              # progress
kubectl patch cronjob bulk-analysis -n prod \
    -p '{"spec":{"suspend":true}}'                         # pause after tonight
kubectl delete job -n prod <job-name>                      # stop tonight's run now
```

Deleting the Job stops it immediately and safely: completed units are already in
the state file, and only in-flight calls are lost.

### Watch on the first morning

- **Did it actually stop?** `kubectl get jobs -n prod` — the Job should be
  Complete, not Running, shortly after 06:00.
- **How far did it get?** The last log lines give `[n/total]`. Divide by 8 hours
  to get a nightly rate, and by that the number of nights the corpus needs.
- **`timeZone` rejected?** Needs GKE 1.27+. Drop the field and express the
  schedule in UTC, and set `TZ` to match whatever `--stop-at` should mean.
- **Pod Pending?** The gVisor toleration is patched in for prod only; a dev run
  needs the same patch.

## Knobs

`--sections risk_analysis,gap_analysis` · `--company-ids 25,58` ·
`--limit N` · `--workers N` (default 4; these are PDF uploads plus long streamed
responses, so the ceiling is the Gemini quota) · `--coverage` ·
`--keep-duplicates` · `--stop-at HH:MM` / `--max-runtime SECONDS` ·
`--years` / `--end-year` / `--history-years` on the cohort builder.
`BULK_ANALYSIS_UID` supplies `--uid` from the environment.
