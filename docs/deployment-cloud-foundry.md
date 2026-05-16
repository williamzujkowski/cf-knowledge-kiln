# Cloud Foundry deployment

This is the operator's guide. For architectural context, see
[architecture.md](./architecture.md) and
[ADR-0004](./adr/0004-cf-process-model.md).

## Prerequisites

- `cf` CLI installed and logged in to your foundation.
- Target org and space already exist (`cf target -o <org> -s <space>`).
- A Postgres service plan available — locally we use the user's
  [`cf-local-service-broker`](https://github.com/williamzujkowski/cf-local-service-broker)
  which exposes a `postgresql` service. Any CF Postgres broker works.

## One-time setup

```bash
# Create the bound Postgres service. The name is the binding key the
# app expects; you can change it but must also change KILN_PG_SERVICE_NAME.
cf create-service postgresql shared cf-knowledge-kiln-db

# Verify it's up.
cf services
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
