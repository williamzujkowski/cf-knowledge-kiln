---
title: Restart the ingestion worker
status: active
owner: platform
doc_type: runbook
sensitivity: internal
last_reviewed: 2026-05-10
tags: [runbook, ingestion, worker, ops]
---

## Restart the ingestion worker

When `cf-knowledge-kiln-worker` stops processing the queue but is still
reporting `Up` in `cf apps`, the most common cause is a stuck DB
connection. Restart the worker process; it will recover idle jobs on
boot via `IngestionWorker.recover_stale_running` (see
`src/cf_knowledge_kiln/ingestion/worker.py`).

## Symptoms

- `ingestion_runs` has rows with `state = running` and `started_at`
  older than five minutes.
- `make run-worker` log shows no `picked_job` events for > 60 seconds.
- `/healthz` on the worker still returns `200` (process is alive,
  loop is stuck).

## Steps

1. `cf restart cf-knowledge-kiln-worker` — single instance, no traffic
   to drain because `no-route: true`.
2. Watch the boot logs for one minute. You should see
   `recover_stale_running marked N rows back to queued` followed by
   `picked_job` events.
3. If no `picked_job` events fire within two minutes, escalate per
   `sop-requeue-stuck-ingest-job`.

## Why not just `cf scale`?

Scaling to zero then back to one triggers a fresh provisioning cycle
that re-reads the service binding. The restart path is faster and
preserves the binding handle; only fall back to `scale --instances 0`
if the binding itself looks stale.
