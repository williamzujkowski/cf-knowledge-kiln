# Cloud Foundry deployment

This is the operator's guide. For architectural context, see
[architecture.md](./architecture.md) and
[ADR-0004](./adr/0004-cf-process-model.md).

## Prerequisites

- `cf` CLI installed and logged in to your foundation.
- Target org and space already exist (`cf target -o <org> -s <space>`).
- A **pgvector-enabled** Postgres reachable from your CF org/space.
  Per [ADR-0002](./adr/0002-postgres-pgvector.md) / [ADR-0008](./adr/0008-pgvector-mvp-critical.md),
  kiln's MVP uses hybrid retrieval (pgvector + FTS); the bound
  database must have `CREATE EXTENSION vector` already run against
  it. The app does not have CREATE EXTENSION privilege at runtime,
  by design.

### Recommended path (homelab BOSH foundation)

1. Deploy [`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release)
   on the BOSH director per its
   [#3 runbook](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3).
2. Push [`cf-local-service-broker`](https://github.com/williamzujkowski/cf-local-service-broker)
   as a CF app, point it at the pgvector Postgres VM, register with CF.
3. Bind kiln:

   ```bash
   cf create-service postgresql-local pgvector cf-knowledge-kiln-db
   cf bind-service cf-knowledge-kiln-api    cf-knowledge-kiln-db
   cf bind-service cf-knowledge-kiln-worker cf-knowledge-kiln-db
   ```

The broker creates a database AND runs `CREATE EXTENSION vector` at
provision time. The app reads `VCAP_SERVICES` and connects.

### Alternative paths

Any pgvector-capable Postgres works as long as the bound database has
the extension installed:

```bash
# Broker (other flavor) with a pgvector plan:
cf create-service <broker> pgvector cf-knowledge-kiln-db

# User-provided service over a managed cloud Postgres (RDS / Cloud SQL /
# Crunchy Bridge) or a container you operate yourself:
cf cups cf-knowledge-kiln-db -p '{"uri":"postgres://USER:PASSWORD@HOST:5432/DBNAME"}'  <!-- pragma: allowlist secret -->
# (Run `CREATE EXTENSION vector` against the database before binding.)
```

The bound service name (`cf-knowledge-kiln-db`) must match
`KILN_PG_SERVICE_NAME`. Change both together if you rename.

### Local dev

For local development and CI, use the pgvector Docker image directly:

```bash
docker run -d --name kiln-pg \
  -e POSTGRES_PASSWORD=kiln \
  -e POSTGRES_USER=kiln \
  -e POSTGRES_DB=kiln \
  -p 5432:5432 \
  pgvector/pgvector:pg16

export KILN_DATABASE_URL=postgresql+asyncpg://kiln:kiln@localhost:5432/kiln  # pragma: allowlist secret
make migrate
make run
```

## Push

```bash
cf push -f manifest.yml --strategy rolling
```

`--strategy rolling` keeps the previous app running until the new instance passes healthchecks, so a botched buildpack upgrade or a regression in `/readyz` can't tear down the live API mid-deploy. The Concourse `deploy-cf` job uses the same flag. Drop `--strategy rolling` for the first ever push (when there's nothing live to roll over).

### Dep installation (`requirements.txt`)

The repo ships a top-level `requirements.txt` whose only line is `-e .[db,ingestion,real-embeddings]`. It exists for the CF `python_buildpack` 1.8.x family, which doesn't yet support PEP 621 `pyproject.toml`-only installs — without the file, the buildpack runs `pip install` against an empty target and the API crash-loops with `No module named uvicorn` (#229).

Local dev installs still go through `make bootstrap` / `pip install -e .[dev,db,ingestion,embeddings]`; the `requirements.txt` is only for the buildpack staging path. Once `cloudfoundry/python-buildpack` ships PEP 621 support, the file can be removed.

For deployments where zero downtime matters more than the simpler ops surface, swap in a blue-green pattern (`cf push <name>-green` → swap routes → `cf delete <name>-blue`) or the `cf-cli` blue-green plugin.

This deploys two apps:

- `cf-knowledge-kiln-api` — HTTP, with route, health check at `/healthz`.
- `cf-knowledge-kiln-worker` — `no-route: true`, process health check.

Both apps are bound to `cf-knowledge-kiln-db` per the manifest. They
read connection info from `VCAP_SERVICES` at startup (Phase 2+).

### Sizing notes (#242)

The shipped manifest sizes are tuned for the `local-sentence-transformers`
provider on a typical CF cell with no GPU. Three things drive the
numbers:

| App | Setting | Why |
| --- | --- | --- |
| api | `disk_quota: 2G` | The droplet stages sentence-transformers + torch+cpu + the kiln package (~1.3 GB installed). 1G fails on `libtorch_cpu.so` extraction during the copy-in step. |
| worker | `memory: 2G` | At peak the worker holds the embedding model (~134 MB e5 / ~500 MB Nomic) + torch+cpu runtime (~500 MB resident) + batch activations. At the previous 1G the cgroup fired with exit 137 on the first batch. |
| worker | `disk_quota: 2G` | Same staging reason as the api. |
| worker | `KILN_INGEST_CONCURRENCY: 1` | Higher concurrency just multiplies peak RAM under the 2G cap. Raise for high-throughput corpora deployed with more memory. |
| both | `--extra-index-url` for CPU torch | `requirements.txt` adds the PyTorch CPU index so pip resolves `torch+cpu` (~200 MB) instead of the default CUDA build (~2.7 GB of NVIDIA libs that won't fit the staging container). Local-dev installs via `pip install -e .[real-embeddings]` go through pyproject.toml and are unaffected. |

A foundation with a `default_app_disk_in_mb` lower than 2048 will refuse this manifest — escalation in that case is to bump the foundation default. Tracked in homelab-iac#700 for the homelab CF foundation.

### First-deploy schema bootstrap (#244)

The API and worker both run `alembic upgrade head` at startup by
default. A first `cf push` against a freshly-provisioned database
applies the schema with no operator action — no `cf ssh ... make
migrate` step needed. A Postgres transaction-level advisory lock
serializes both apps so they can't race on `alembic_version`.

Opt-out for shared-DB deployments (where another process owns the
schema, or you prefer the explicit migration step):

```bash
cf set-env cf-knowledge-kiln-api    KILN_AUTO_MIGRATE_ON_STARTUP false
cf set-env cf-knowledge-kiln-worker KILN_AUTO_MIGRATE_ON_STARTUP false
cf restage cf-knowledge-kiln-api
cf restage cf-knowledge-kiln-worker
```

Constraint inherited from the migrate-first model: every revision in
`alembic/versions/` must be backward-compatible with the previously
deployed app version. The rolling deploy (`--strategy rolling` above)
leaves the old app instance serving traffic while the new one
migrates; a destructive DDL change breaks the old instance until the
roll completes. Stage breaking schema changes as expand → contract
across two deploys.

## Environment variables

Set sensitive values via `cf set-env`, never in the manifest:

```bash
cf set-env cf-knowledge-kiln-api KILN_BEARER_TOKEN '<value>'
cf set-env cf-knowledge-kiln-api KILN_EMBEDDING_API_KEY '<value>'
cf restage cf-knowledge-kiln-api
```

Full env reference: [configuration.md](./configuration.md).

## Rate limiting

The API has an in-process per-IP token-bucket gate on `/v1/search`,
`/v1/agent/context-pack`, `/search`, and `/feedback`. Both buckets are
configurable via env:

```bash
cf set-env cf-knowledge-kiln-api KILN_RATE_LIMIT_SEARCH_PER_MIN '60'
cf set-env cf-knowledge-kiln-api KILN_RATE_LIMIT_FEEDBACK_PER_MIN '30'
# Trust CF gorouter's X-Forwarded-For for client-IP keying.
cf set-env cf-knowledge-kiln-api KILN_TRUST_FORWARDED_FOR 'true'
cf restage cf-knowledge-kiln-api
```

Caveats:

- **In-process, single-instance only.** Buckets live in the dyno's
  memory. If you `cf scale -i 2`, each instance enforces its own
  limit, so the effective ceiling is `instances × per_min`. Real
  multi-instance rate limiting needs a shared backend (separate
  follow-up; not in scope for the MVP gate).
- `KILN_TRUST_FORWARDED_FOR` must be **off** for any deployment where
  callers can reach the dyno directly (no upstream proxy stripping
  XFF). In CF behind the gorouter, leave it on so per-IP keying tracks
  the real client and not all-of-gorouter.
- The limiter caps its bucket dict at 50k distinct keys with LRU
  eviction. An attacker spraying random XFF values will not exhaust
  memory; an evicted bucket simply resets to full on its next hit.

Rate-limit responses carry `Retry-After: <seconds>`. JSON routes
return `429 Too Many Requests`; the HTMX form routes (`/search`,
`/feedback`) render a swap-friendly fragment with the same status, so
the UI shows the limit message inline instead of a silent no-op.

## Health checks

| Endpoint   | Used for           | What it checks                                  |
| ---------- | ------------------ | ----------------------------------------------- |
| `/healthz` | CF liveness        | Process is up. No I/O. Returns `200` always.    |
| `/readyz`  | Load-balancer LB   | DB ping (Phase 2+), provider ping (Phase 4+).   |
| `/version` | Observability      | Returns the package version string.             |

The manifest points CF's HTTP health check at `/healthz` with a 10-
second invocation timeout. If you customize, keep `/healthz` cheap.

## Internal route deployment (apps.internal)

For deployments where only other CF apps in the same foundation should
reach kiln (e.g., an agent host that consumes `/v1/agent/context-pack`),
prefer an `apps.internal` route to a public gorouter route. Traffic
stays inside the CF overlay network and never traverses the public
Diego edge.

**Push without a public route:**

```bash
cf push -f manifest.yml --no-route
cf map-route cf-knowledge-kiln-api apps.internal --hostname cf-knowledge-kiln-api
```

The internal route resolves DNS-style from inside the same CF org
(`cf-knowledge-kiln-api.apps.internal`). Resolution from outside the
foundation will not work — that's the point.

**Open a network policy so consumer apps can reach kiln:**

`apps.internal` routes are deny-by-default at the container-network
layer. Each consumer app needs an explicit policy:

```bash
# Allow `my-agent-app` to call kiln on port 8080.
cf add-network-policy my-agent-app \
    --destination-app cf-knowledge-kiln-api \
    --protocol tcp \
    --port 8080
```

Verify with `cf network-policies`. Without this, the consumer app gets
a connection-refused at TCP level (visible as a httpx `ConnectError`
or similar in its logs).

**Consumer apps still need a bearer token.** Network-layer isolation
is necessary but not sufficient — `KILN_AUTH_MODE=bearer` (the
production default; see `manifest.yml`) still requires the
`Authorization: Bearer <token>` header on every `/v1/*` request. Bind
the token via a CF user-provided service so each consumer reads it
from its own `VCAP_SERVICES` rather than its own env:

```bash
cf cups kiln-token -p '{"token": "<generated>"}'
cf bind-service my-agent-app kiln-token
cf restart my-agent-app
```

In the consumer app, read `VCAP_SERVICES.user-provided[].credentials.token`.

**Healthz from inside the foundation:**

```bash
cf ssh my-agent-app -c 'curl -fsS http://cf-knowledge-kiln-api.apps.internal:8080/healthz'
```

The port (`8080`) is the container port from `manifest.yml`, not the
public 443/80 the gorouter uses. `apps.internal` traffic is plain
HTTP — no TLS terminator sits in front, so requests inside the
foundation use `http://`.

## Scaling

The API scales horizontally (`cf scale cf-knowledge-kiln-api -i 3`).
The worker should usually stay at one instance until you have an
explicit reason — running multiple workers requires the Phase 3 job
queue to coordinate them.

## Smoke test after push

A canonical `scripts/smoke-test.sh` walks `/healthz`, `/version`,
`/readyz`, and a real `/v1/search` round-trip. It honors
`KILN_AUTH_MODE=bearer` (the production default) by attaching the
`Authorization` header when `KILN_BEARER_TOKEN` is set.

```bash
APP_URL="https://$(cf app cf-knowledge-kiln-api | awk '/routes:/ {print $2}')"
TOKEN="$(cf env cf-knowledge-kiln-api | awk '/KILN_BEARER_TOKEN/ {print $2}')"

KILN_URL="$APP_URL" KILN_BEARER_TOKEN="$TOKEN" \
    ./scripts/smoke-test.sh
```

Quick one-liner without the script (skips the search round-trip):

```bash
curl -fsS "$APP_URL/healthz"   # expect 200 {"status":"ok",...}
curl -fsS "$APP_URL/readyz"    # expect 200 or 503 {"checks":{...}}
curl -fsS "$APP_URL/version"   # expect 200 {"version":"0.1.0"}
```

For an apps.internal-only deployment, smoke-test from inside the
foundation via `cf ssh`:

```bash
cf ssh my-agent-app -c '
  TOKEN=$(jq -r ".\"user-provided\"[0].credentials.token" <<< "$VCAP_SERVICES")
  KILN_URL=http://cf-knowledge-kiln-api.apps.internal:8080 \
  KILN_BEARER_TOKEN="$TOKEN" \
  /path/to/cloned/cf-knowledge-kiln/scripts/smoke-test.sh
'
```

## Logs

```bash
cf logs cf-knowledge-kiln-api --recent
cf logs cf-knowledge-kiln-worker --recent
```

## Common operations

| Need                          | Command                                                |
| ----------------------------- | ------------------------------------------------------ |
| Restart after env change      | `cf restage cf-knowledge-kiln-api`                     |
| Recreate from a fresh build   | `cf push -f manifest.yml`                              |
| Run a one-off task            | `cf run-task cf-knowledge-kiln-worker --command '...'` |
| SSH into a container          | `cf ssh cf-knowledge-kiln-api`                         |
| Tail recent logs              | `cf logs cf-knowledge-kiln-api --recent`               |

## First-start: pre-warm the embedding model (#198)

If you're deploying with the `local-sentence-transformers` embedding
provider, the very first container start downloads the model weights
from HuggingFace on demand. The startup health probe times out by
default at 90 seconds (`KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS`); cold
downloads of larger models (`nomic-embed-text-v1.5`, the MVP default
in `config/models.example.yaml`, is ~500 MB) over a slow link can
exceed that. When the probe trips, `/readyz` is pinned to
`embedding: failing` for the life of the process and `/v1/search`
returns 503 indefinitely.

Two ways to avoid it:

1. **Pre-warm**. Before the first `cf push`, prime the HuggingFace
   cache on the box you're pushing from. Use the model your
   `config/models.yaml` references — for the MVP default that's:

   ```bash
   .venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
     SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', \
       trust_remote_code=True, device='cpu').encode(['x'])"
   ```

   Whether the CF buildpack stages your local `~/.cache/huggingface/`
   into the dyno depends on the buildpack version and foundation
   policy — test on your foundation. If the cache isn't preserved
   across `cf push`, the pre-warm has to happen on the dyno itself
   (option 2).

2. **Bump the timeouts** for the first deploy. The embedding probe
   timeout AND the manifest's `timeout:` need to move together — the
   probe runs inside the app startup window:

   ```bash
   cf set-env cf-knowledge-kiln-api KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS 600
   # And bump the manifest startup timeout to match, e.g. 660 (probe + 60s margin):
   #   timeout: 660
   cf restage cf-knowledge-kiln-api
   ```

   Six hundred seconds is enough to download `nomic-embed-text-v1.5`
   (~500 MB) over a constrained link. After the first successful
   start the weights cache to disk; you can lower both values again.

If `/readyz` ends up pinned at `embedding: failing` from a tripped
probe, the fix is `cf restart cf-knowledge-kiln-api` — the next start
hopefully finds the weights warm on disk (provided the dyno's
filesystem is persistent on your foundation) and the probe returns in
milliseconds.

## Troubleshooting

When something is broken — `/readyz` degraded, a stuck ingestion job,
a crashed worker, a misconfigured embedding provider — see the
[troubleshooting runbook](./troubleshooting.md). It is organized as
Symptom → Diagnosis → Action per failure mode.
