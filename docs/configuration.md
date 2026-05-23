# Configuration

Settings come from three places, in this precedence (highest wins):

1. **Environment variables** (prefix `KILN_`).
2. **`.env`** file in the working directory (development only).
3. **Defaults** in `src/cf_knowledge_kiln/config/settings.py`.

Secret-bearing fields are env-only. They never appear in YAML or in
the manifest.

## Env-var reference

| Variable                              | Default                  | Purpose                                                            |
| ------------------------------------- | ------------------------ | ------------------------------------------------------------------ |
| `KILN_APP_NAME`                       | `cf-knowledge-kiln`      | Used in logs and the FastAPI title.                                |
| `KILN_ENV`                            | `development`            | One of `development`, `staging`, `production`.                     |
| `KILN_LOG_LEVEL`                      | `INFO`                   | `DEBUG` / `INFO` / `WARNING` / `ERROR`.                            |
| `KILN_HTTP_PORT`                      | `8080`                   | Local bind port. CF overrides with `$PORT`.                        |
| `KILN_WEB_WORKERS`                    | `2`                      | uvicorn worker processes for the API (read by `scripts/start-api.sh`). |
| `KILN_DATABASE_URL`                   | *(unset in CF)*          | Direct DB URL. In CF, leave unset and bind a Postgres service.     |
| `KILN_PG_SERVICE_NAME`                | `cf-knowledge-kiln-db`   | Name to look up in `VCAP_SERVICES`.                                |
| `KILN_PG_POOL_SIZE`                   | `5`                      | Connection pool size.                                              |
| `KILN_PG_POOL_MAX_OVERFLOW`           | `10`                     | Pool overflow.                                                     |
| `KILN_EMBEDDING_API_KEY`              | —                        | Secret. Set via `cf set-env` or env.                               |
| `KILN_EMBEDDING_BASE_URL`             | —                        | Override the OpenAI-compatible embedding base URL.                 |
| `KILN_GENERATOR_API_KEY`              | —                        | Secret.                                                            |
| `KILN_GENERATOR_BASE_URL`             | —                        | Override.                                                          |
| `KILN_INGEST_CONCURRENCY`             | `4`                      | Worker concurrency.                                                |
| `KILN_INGEST_MAX_FILE_BYTES`          | `1048576`                | Files larger than this are skipped with `too_large`.               |
| `KILN_INGEST_EMBED_BATCH_SIZE`        | `32`                     | Chunks per `provider.embed()` call during the ingestion embed pass.|
| `KILN_INGEST_EMBED_CONCURRENCY`       | `4`                      | Max embed batches in flight in parallel. Capped by a semaphore.    |
| `KILN_DEFAULT_MAX_CHUNKS`             | `8`                      | Default retrieval result count.                                    |
| `KILN_DEFAULT_MAX_TOKENS`             | `3000`                   | Default agent token budget.                                        |
| `KILN_DEFAULT_STATUS_PREFERENCE`      | `active,approved`        | Comma-separated.                                                   |
| `KILN_SOURCE_ALLOWLIST_PATH`          | `config/sources.yaml`    | Path to the source allowlist.                                      |
| `KILN_AUTH_MODE`                      | `none`                   | `none` (dev only) / `bearer` / `mtls`.                             |
| `KILN_BEARER_TOKEN`                   | —                        | Required when `KILN_AUTH_MODE=bearer`.                             |
| `KILN_OTEL_EXPORTER_OTLP_ENDPOINT`    | —                        | OTLP-HTTP traces endpoint. Set + install the `[otel]` extra to enable tracing; unset = no tracing (zero overhead). See [docs/observability.md](./observability.md). |
| `KILN_OTEL_SERVICE_NAME`              | `cf-knowledge-kiln`      | OpenTelemetry `service.name` reported with every span.             |

## Embedding fan-out (`KILN_INGEST_EMBED_*`)

The ingestion pipeline batches chunks into groups of
`KILN_INGEST_EMBED_BATCH_SIZE` and runs up to
`KILN_INGEST_EMBED_CONCURRENCY` batches in parallel against the
configured embedding provider. Defaults: `32` and `4`.

Tuning guidance:

* `LocalSentenceTransformersProvider` (CPU): the model owns its own
  thread pool via `OMP_NUM_THREADS`. Total worker threads is roughly
  `OMP_NUM_THREADS × KILN_INGEST_EMBED_CONCURRENCY`. The defaults
  assume `OMP_NUM_THREADS=2` and `KILN_INGEST_EMBED_CONCURRENCY=4` —
  so 8 worker threads, which is reasonable on a 4-to-8-core box.
  Going wider on either dimension oversubscribes the CPU and the
  batches start blocking each other.
* `OpenAICompatibleEmbeddingProvider`: each batch is one HTTP
  request, so concurrency is really "in-flight HTTP requests".
  The provider's own per-request rate limit dominates; if you see
  429s, lower `KILN_INGEST_EMBED_CONCURRENCY`.
* `MockEmbeddingProvider`: deterministic, no I/O. The defaults are
  fine; raising concurrency past `4` gains nothing.

The fan-out preserves output order — chunk `N` always gets the
vector that came back for input `N`, regardless of which batch
finished first. See
`src/cf_knowledge_kiln/ingestion/embedding/batched.py` for the
helper, and `tests/unit/test_ingestion_pipeline_batched_embed.py`
for the order-preservation test.

**Per-batch failure granularity (issue #151):** when an ingest run
hits a transient provider failure on one batch, the batches that
succeeded persist — their vectors are written to the embeddings
table and counted in `summary.embeddings_created`. Only the failed
batches' chunks are accounted as `summary.embeddings_failed`, and
each failure is logged once with structured fields (`start_offset`,
`batch_size`, `exc_class`, `exc_message`) so operators can find the
offending batch without grepping a full traceback. The run only
fails outright when EVERY batch failed — at that point there is no
partial-success work worth committing, so the loud-failure
semantics still hold. Before #151 a single bad batch would discard
every sibling batch's successful work.

## Connection pool sizing (`KILN_PG_POOL_*`)

Each Python process opens up to `KILN_PG_POOL_SIZE +
KILN_PG_POOL_MAX_OVERFLOW` connections to Postgres — the defaults are
`5 + 10 = 15` per process. `Database.__init__` logs these at startup
(`starting Postgres engine: ... (pool_size=5, max_overflow=10)`).

The connection total to budget against the database's
`max_connections` is **per process, summed across every process**:

```text
total ≈ KILN_WEB_WORKERS × (pool_size + max_overflow)   # the api app
      + 1              × (pool_size + max_overflow)      # the worker app
```

`KILN_WEB_WORKERS` is the uvicorn worker count for the api app
(default 2; see the env-var table). The worker app runs a single
process.

**Worked example.** With the defaults and `KILN_WEB_WORKERS=4`:
`4 × 15 + 1 × 15 = 75` connections. A managed Postgres plan that caps
`max_connections` at 100 has only ~25 left for migrations, `psql`
sessions, and monitoring — tight. Raise `KILN_WEB_WORKERS` further and
the app deadlocks on pool exhaustion under load: it starts cleanly,
then stalls — a failure mode with no obvious error.

**Recommendation for multi-worker CF deployments.** Lower the
per-process pool — `KILN_PG_POOL_SIZE=2` keeps each process at
`2 + 10 = 12` and leaves `max_overflow` as burst headroom. Size it so
`(KILN_WEB_WORKERS + 1) × (pool_size + max_overflow)` stays a
comfortable margin under the database's `max_connections`. Confirm the
plan's `max_connections` with `SHOW max_connections;`.

## YAML config files

These live under `config/` and are loaded at boot. The `.example`
versions are committed; copy them to non-`.example` names locally.
The non-example files are gitignored.

| File                            | Purpose                                                    |
| ------------------------------- | ---------------------------------------------------------- |
| `config/models.yaml`            | Active embedding + generator model + provider settings.    |
| `config/sources.yaml`           | Allowlisted ingestion sources.                             |
| `config/security.yaml`          | Content filters, freshness thresholds, retrieval weights.  |

## Adding a setting

1. Add the field to `Settings` in `src/cf_knowledge_kiln/config/settings.py`.
2. Add a `KILN_*` entry to `.env.example`.
3. Add a row to the table above.
4. Add a unit test in `tests/unit/test_settings.py`.

`KILN_` prefix is mandatory — it scopes the namespace and prevents
collisions in CF runtime environments that already set `PORT`, `HOME`,
`PATH`, etc.
