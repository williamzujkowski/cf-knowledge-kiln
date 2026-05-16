# Cloud Foundry deployment

This is the operator's guide. For architectural context, see
[architecture.md](./architecture.md) and
[ADR-0004](./adr/0004-cf-process-model.md).

## Prerequisites

- `cf` CLI installed and logged in to your foundation.
- Target org and space already exist (`cf target -o <org> -s <space>`).
- A **pgvector-enabled** Postgres reachable from your CF org/space.
  Phase 2's migrations require `CREATE EXTENSION IF NOT EXISTS vector`
  to have already succeeded against the bound database — the app does
  not have CREATE EXTENSION privilege at runtime, by design.

### Picking a Postgres path

Two flavors, depending on what your foundation already exposes:

1. **Postgres service broker with a pgvector plan.** Whoever runs the
   broker is responsible for `CREATE EXTENSION vector` at provision
   time. You bind normally:

   ```bash
   cf create-service <broker> pgvector cf-knowledge-kiln-db
   ```

2. **User-provided service over a standalone pgvector Postgres.** You
   point CF at an out-of-band-managed database (BOSH-deployed VM,
   Incus/Podman container on a Pi, managed cloud DB, etc.):

   ```bash
   cf cups cf-knowledge-kiln-db -p '{"uri":"postgres://user:pass@host:5432/dbname"}'
   ```

   You're on the hook for installing pgvector on that Postgres before
   binding.

For the homelab BOSH-deployed CF specifically, there is no off-the-shelf
pgvector-enabled BOSH Postgres release as of 2026-05; the infra decision
is tracked in [issue #35](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/35).

## One-time setup

```bash
# Pick one of the two paths above, then:
cf bind-service cf-knowledge-kiln-api    cf-knowledge-kiln-db
cf bind-service cf-knowledge-kiln-worker cf-knowledge-kiln-db
cf services
```

The bound service name (`cf-knowledge-kiln-db`) must match
`KILN_PG_SERVICE_NAME`. Change both together if you rename.

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
