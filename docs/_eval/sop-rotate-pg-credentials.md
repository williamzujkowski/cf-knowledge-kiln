---
title: "SOP: rotate the Postgres service binding credentials"
status: active
owner: platform
doc_type: sop
sensitivity: internal
last_reviewed: 2026-04-09
tags: [sop, postgres, credentials, rotation, cloud-foundry]
---

## SOP: rotate Postgres credentials

CF service brokers rotate Postgres credentials by issuing a new
binding and dropping the old. The kiln reads the binding via
`VCAP_SERVICES`; the rotation procedure is a controlled rebind on
both apps.

## Steps

Create the replacement binding:

```bash
cf bind-service cf-knowledge-kiln-api kiln-pg --binding-name kiln-pg-2
cf bind-service cf-knowledge-kiln-worker kiln-pg --binding-name kiln-pg-2
```

Restage both apps so the new binding is read:

```bash
cf restage cf-knowledge-kiln-api
cf restage cf-knowledge-kiln-worker
```

Verify connectivity: `curl https://<api-route>/readyz` returns 200 and
the worker log shows `picked_job` events. Then drop the old binding:

```bash
cf unbind-service cf-knowledge-kiln-api kiln-pg --binding-name kiln-pg
cf unbind-service cf-knowledge-kiln-worker kiln-pg --binding-name kiln-pg
```

Restart both apps one more time so the binding map updates:

```bash
cf restart cf-knowledge-kiln-api
cf restart cf-knowledge-kiln-worker
```

## Verification

- `/readyz` returns 200 on the api.
- `ingestion_runs` continues to advance.
- `psql` against the new binding succeeds; against the old fails.

## Failure mode

If the restage triggers a crashloop, the new binding probably
isn't pre-populated with pgvector. See
`runbook-pgvector-extension-missing.md`.
