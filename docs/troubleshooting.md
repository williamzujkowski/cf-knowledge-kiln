# Troubleshooting

Operator runbook for cf-knowledge-kiln. Each section is **Symptom →
Diagnosis → Action**. For deployment see
[deployment-cloud-foundry.md](./deployment-cloud-foundry.md); for
configuration see [configuration.md](./configuration.md).

First stop for any "is it healthy?" question:

```bash
curl -s https://<app-route>/readyz | jq
```

`/healthz` is liveness only (always 200 if the process is up).
`/readyz` reports per-dependency health and returns **503** when
degraded.

---

## `/readyz` reports `degraded`

`/readyz` returns a per-dependency `checks` map and a roll-up
`status`. Only a `failing` check degrades readiness; `not_configured`
is informational.

```json
{ "status": "degraded", "checks": { "postgres": "ok", "embedding": "failing" } }
```

### `postgres: failing`

**Diagnosis.** The DB pool is unconfigured (no `KILN_DATABASE_URL`, no
`VCAP_SERVICES` binding) or a `SELECT 1` round-trip failed.

**Action.** Confirm the service binding: `cf services` then
`cf env cf-knowledge-kiln-api` and check `VCAP_SERVICES` carries the
Postgres credentials. If you set `KILN_DATABASE_URL` directly, verify
it is reachable from the app container. The process stays up (liveness
is unaffected) — fix the binding and the next `/readyz` poll clears.

### `embedding: failing`

**Diagnosis.** A provider was configured but the one-shot startup
probe (`/v1/search` needs it) failed. Common causes: a typo in
`KILN_EMBEDDING_BASE_URL`, an unreachable embedding service, a model
name that does not resolve, a missing `trust_remote_code` flag for a
model that needs it, or an embedding-dimension mismatch.

**Action.** Read the app startup logs — the probe logs the exception
(`embedding provider health probe failed`). Fix `config/models.yaml`
or the `KILN_EMBEDDING_*` env vars and restage. See
[model-providers.md](./model-providers.md) for the per-model knobs
(`name`, `dimensions`, `trust_remote_code`, `normalize`).

### `embedding: not_configured`

**Not an error.** No `config/models.yaml` is present, so retrieval
runs FTS-only (lexical, no vector arm). `/readyz` stays `ready`. If
you intended hybrid retrieval, add the embedding config and restage.

---

## Ingestion worker crashed mid-job

**Symptom.** The worker process died (OOM, SIGKILL, container
recycle) while a job was running.

**Diagnosis.** Jobs in the `ingestion_jobs` table left in `running`
state are orphaned — no live worker owns them.

**Action — just restart the worker.** Do **not** manually mark jobs
failed. On startup the worker runs a recovery sweep
(`_recover_stale_running`) over every `running` job:

- `result_run_id` set **and** the referenced `ingestion_runs` row is
  `succeeded` → the work finished durably before the crash; the job
  is marked `succeeded` (not redone).
- `result_run_id` set but the run is `running`/`failed`, **or**
  `result_run_id` is unset → the work did not finish; the job is
  requeued.

The startup log reports `requeued N orphaned running job(s) at
startup`. Because `run_source` commits the `ingestion_runs` row before
the job is marked done, a crash in that window is recognized and the
job is completed rather than duplicated.

---

## Stuck or failed ingestion jobs

**Symptom.** A source is not getting indexed; jobs sit in `queued` or
land in `failed`.

**Diagnosis.** Inspect the queue:

```sql
SELECT id, source_id, status, attempts, last_error, enqueued_at
FROM ingestion_jobs ORDER BY enqueued_at DESC LIMIT 20;
```

Status values: `queued` → `running` → `succeeded` | `failed`. Workers
claim the oldest `queued` row with `SELECT ... FOR UPDATE SKIP
LOCKED`, so multiple workers never double-process a row. A `running`
row with no live worker is handled by the recovery sweep above.

**Action.** For a `failed` job that is retryable (transient DB or
embedding error — check `last_error`), requeue it: a `running`-state
worker exposes `IngestionJobsRepository.requeue`, which clears the
timestamps and returns the job to `queued`. A job that fails
repeatedly on the same `last_error` is a real defect — fix the source
or the config rather than requeuing.

---

## Slow or failing embedding provider

**Symptom.** Ingestion runs are slow, or `ingestion_runs.errors`
records embedding failures.

**Diagnosis — distinguish two cases:**

- **Rate-limited** — errors mention HTTP 429 / batch timeouts. The
  `openai-compatible` adapter retries 408/425/429/5xx with
  exponential backoff + jitter; sustained 429s mean the provider's
  quota is the bottleneck.
- **Unreachable** — connection-refused / DNS / TLS errors. The base
  URL or network path is wrong.

The embedding pass fails per-batch, not all-or-nothing (#151): a
partial failure records breadcrumbs in `ingestion_runs.errors` and
the run status is `partial`; only an all-batches failure fails the
run.

**Action.** For rate limits, lower `KILN_INGEST_EMBED_CONCURRENCY` or
slow the source cadence. For unreachable, fix `KILN_EMBEDDING_BASE_URL`
/ the binding. For a `local` provider, confirm the model weights are
cached and the container has enough memory.

---

## Allowlist or config validation

**Symptom.** You changed `config/sources.yaml` and want to confirm it
is valid before a worker picks it up.

**Action.** Validate without restarting anything:

```bash
python -m cf_knowledge_kiln.ingestion validate --config config/sources.yaml
```

`validate` is the cheap fail-fast — it parses the source allowlist,
rejects duplicate names and malformed entries, and exits non-zero on
any problem. Run it in CI / pre-deploy.

---

## pgvector extension missing on boot

**Symptom.** The app or a migration fails with `extension "vector" is
not available` or similar.

**Diagnosis.** The bound Postgres role does **not** have
`CREATE EXTENSION` privilege. Migration `0001` assumes the `vector`
extension is already installed — it does not install it.

**Action.** The extension is installed at **provision time** by the
service broker / operator, not by the app at runtime. Confirm the
Postgres service was provisioned with pgvector (see
[deployment-cloud-foundry.md](./deployment-cloud-foundry.md) for the
`bosh-pgvector-release` + broker path). Re-binding the app does not
help — the extension must exist in the database first.
