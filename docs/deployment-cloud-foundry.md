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

For deployments where zero downtime matters more than the simpler ops surface, swap in a blue-green pattern (`cf push <name>-green` → swap routes → `cf delete <name>-blue`) or the `cf-cli` blue-green plugin.

This deploys two apps:

- `cf-knowledge-kiln-api` — HTTP, with route, health check at `/healthz`.
- `cf-knowledge-kiln-worker` — `no-route: true`, process health check.

Both apps are bound to `cf-knowledge-kiln-db` per the manifest. They
read connection info from `VCAP_SERVICES` at startup (Phase 2+).

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
