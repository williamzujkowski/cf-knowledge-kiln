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
cf push -f manifest.yml
```

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

## Health checks

| Endpoint   | Used for           | What it checks                                  |
| ---------- | ------------------ | ----------------------------------------------- |
| `/healthz` | CF liveness        | Process is up. No I/O. Returns `200` always.    |
| `/readyz`  | Load-balancer LB   | DB ping (Phase 2+), provider ping (Phase 4+).   |
| `/version` | Observability      | Returns the package version string.             |

The manifest points CF's HTTP health check at `/healthz` with a 10-
second invocation timeout. If you customize, keep `/healthz` cheap.

## Internal route deployment

If you only want internal access, change the route at push time:

```bash
cf push -f manifest.yml --no-route
cf map-route cf-knowledge-kiln-api apps.internal --hostname cf-knowledge-kiln-api
```

## Scaling

The API scales horizontally (`cf scale cf-knowledge-kiln-api -i 3`).
The worker should usually stay at one instance until you have an
explicit reason — running multiple workers requires the Phase 3 job
queue to coordinate them.

## Smoke test after push

```bash
APP_URL="https://$(cf app cf-knowledge-kiln-api | awk '/routes:/ {print $2}')"

curl -fsS "$APP_URL/healthz"   # expect 200 {"status":"ok",...}
curl -fsS "$APP_URL/readyz"    # expect 200 {"status":"ready",...}
curl -fsS "$APP_URL/version"   # expect 200 {"version":"0.1.0"}
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
