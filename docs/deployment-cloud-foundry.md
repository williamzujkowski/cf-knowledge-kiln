# Cloud Foundry deployment

This is the operator's guide. For architectural context, see
[architecture.md](./architecture.md) and
[ADR-0004](./adr/0004-cf-process-model.md).

## Prerequisites

- `cf` CLI installed and logged in to your foundation.
- Target org and space already exist (`cf target -o <org> -s <space>`).
- A **standard Postgres** reachable from your CF org/space. No special
  extensions required for the MVP — [ADR-0007](./adr/0007-fts-first-embeddings-deferred.md)
  defers embeddings (and therefore pgvector) until Phase 5.5. Any CF
  Postgres binding works: a broker-provided service, a UPSI, a
  BOSH-deployed Postgres, or a managed cloud DB.

### Binding Postgres

```bash
# If your foundation has a Postgres broker:
cf create-service <broker> <plan> cf-knowledge-kiln-db

# Or a user-provided service over an out-of-band Postgres:
cf cups cf-knowledge-kiln-db -p '{"uri":"postgres://user:pass@host:5432/dbname"}'

# Either way:
cf bind-service cf-knowledge-kiln-api    cf-knowledge-kiln-db
cf bind-service cf-knowledge-kiln-worker cf-knowledge-kiln-db
cf services
```

The bound service name (`cf-knowledge-kiln-db`) must match
`KILN_PG_SERVICE_NAME`. Change both together if you rename.

### When Phase 5.5 adds embeddings

The Phase 9 eval harness is the gate. If the eval shows retrieval
quality below target on your corpus, Phase 5.5 adds pgvector. At that
point you'll need a pgvector-enabled Postgres. The companion
[`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release)
provides a BOSH release for this; any other source of pgvector
Postgres (cloud-managed, container, etc.) works too.

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
