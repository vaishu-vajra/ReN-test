# Phase 1 — Scaling the ingestion workers (zero-downtime)

Moves the Celery workers off the tiny `e2-medium` pool (≈0.94 vCPU / 2.87Gi
allocatable, OOM-prone for headless Chromium) onto a right-sized `e2-standard-4`
pool with resource requests and an HPA.

Cluster: `data-ingestion-cluster` · zone `europe-west1-b` · project `my-renx-ai`
Namespace: `prod`

> Do this while **no large ingestion is running** — a rolling reschedule
> interrupts in-flight tasks (they're chunked + rescheduled, but reprocessing a
> chunk can duplicate reports until DataX dedups). Old pool stays as rollback.

---

## 1. Create the render node pool (additive — no impact on running pods)

```bash
gcloud container node-pools create render-pool \
  --cluster=data-ingestion-cluster \
  --zone=europe-west1-b \
  --machine-type=e2-standard-4 \
  --num-nodes=3 \
  --enable-autoscaling --min-nodes=3 --max-nodes=6 \
  --disk-size=50 \
  --scopes=gke-default
```

> Start at **3 nodes** (= the 3 celery replicas, one pod per node at 3-vCPU
> request) so the rollout schedules immediately instead of waiting on the cluster
> autoscaler — otherwise `kubectl rollout status` can time out and fail the build.

- Standard runtime (the current celery pods are **not** gVisor-sandboxed —
  `runtimeClassName` is empty and the nodes are untainted — so this preserves the
  security posture). If you later decide to sandbox untrusted rendering, recreate
  with `--sandbox type=gvisor` and add `runtimeClassName: gvisor` to the pod spec.
- `max-nodes=6` matches the HPA `maxReplicas` (one celery pod per node, since the
  pod requests 3 vCPU). Raise both together if you need more.

Verify:
```bash
kubectl get nodes -l cloud.google.com/gke-nodepool=render-pool
```

## 2. Deploy the manifests (rolling update — no downtime)

Changes in this repo (already applied to the manifests):
- `k8s/overlays/prod/patch-resources-celery.yaml` — requests/limits + `nodeSelector: render-pool`
- `k8s/base/celery-hpa.yaml` — HPA (min 3 / max 6, 70% CPU)
- `DOWNLOAD_WORKERS=2` + `MAX_DOCS_PER_TASK=20` in the prod app-config

Preview, then apply:
```bash
kubectl kustomize k8s/overlays/prod            # sanity check
kubectl apply -k k8s/overlays/prod
```

The Deployment does a `RollingUpdate`: new pods schedule onto `render-pool`, old
`e2-medium` pods drain (Celery finishes/relinquishes current tasks on SIGTERM).
Watch:
```bash
kubectl -n prod rollout status deploy/celery --timeout=300s
kubectl -n prod get pods -l app=celery -o wide      # should now be on render-pool
kubectl -n prod get hpa celery                        # TARGETS should read a % (metrics-server working)
```

If `HPA TARGETS` shows `<unknown>`, metrics-server isn't scraping — the HPA won't
scale. On GKE it's usually on by default; confirm with `kubectl top pods -n prod`.

## 3. Canary before the big run

Run the **3,500-URL** sheet first. Watch for OOMKills and CPU saturation:
```bash
watch kubectl -n prod get pods -l app=celery \
  -o custom-columns='POD:.metadata.name,RST:.status.containerStatuses[0].restartCount,NODE:.spec.nodeName'
kubectl -n prod top pods -l app=celery
```
- **0 restarts** = memory is sized right. If you see `OOMKilled`, lower
  `DOWNLOAD_WORKERS` (e.g. to 1) via the configmap and re-apply — no image rebuild.
- If CPU sits pinned at ~100% of request across all 3 pods, the HPA should add
  replicas (and cluster-autoscaler add nodes) up to 6.

## 4. Tuning knobs (no code deploy needed)

Edit `k8s/overlays/prod/kustomization.yaml` → `configMapGenerator` and re-apply:
- `DOWNLOAD_WORKERS` — threads per task. Effective Chromium/pod = `4 × this`.
  2 → 8 (safe on e2-standard-4). Raise for throughput if RAM headroom allows.
- `MAX_DOCS_PER_TASK` — docs per Celery task (smaller = more even load-spread).

HPA ceiling: bump `maxReplicas` in `celery-hpa.yaml` **and** `--max-nodes` on the
pool together.

---

## Rollback (fast, no downtime)

The old `e2-medium` pool (`wi-pool-storage`) is untouched. To revert:

```bash
# 1. Point celery back at the old pool + drop the resource requests:
git revert <this commit>          # or edit patch-resources-celery.yaml nodeSelector back
kubectl apply -k k8s/overlays/prod
kubectl -n prod rollout status deploy/celery

# 2. (optional) remove the HPA if reverting fully:
kubectl -n prod delete hpa celery

# 3. Once happy on the old pool, delete the render pool:
gcloud container node-pools delete render-pool \
  --cluster=data-ingestion-cluster --zone=europe-west1-b
```

Rollout history / emergency undo:
```bash
kubectl -n prod rollout undo deploy/celery
```

---

## Still out of scope (Phases 3 & 4 — deferred)

DataX still receives **3 calls per document** (upsert + store_report +
store_ingesting_report). At 210k docs that's ~630k calls — **DataX + its Postgres
remain the throughput ceiling** regardless of how far celery scales. Load-test
DataX/DB with the canary before committing to the 210k run.
