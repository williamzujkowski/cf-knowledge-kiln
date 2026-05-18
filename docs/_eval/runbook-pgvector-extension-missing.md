---
title: "Runbook: pgvector extension missing on boot"
status: active
owner: platform
doc_type: runbook
sensitivity: internal
last_reviewed: 2026-04-26
tags: [runbook, postgres, pgvector, boot]
---

## Runbook: pgvector extension missing on boot

The kiln refuses to start (`cf-knowledge-kiln-api` crashloops) with
the error `pgvector extension not available on this database`. This
is the canonical preflight failure on a freshly-provisioned Postgres
binding.

## Symptoms

- `cf app cf-knowledge-kiln-api` shows `state=crashed` after the
  first boot attempt.
- `cf logs --recent` carries the line `extension "vector" does not
  exist; refusing to start under AGENTS.md "refuses cleanly when
  pgvector is unavailable" rule`.
- `cf services` shows the Postgres binding as healthy.

## Cause

The kiln requires the `vector` extension at `CREATE EXTENSION
vector` level. Some Postgres service brokers ship pgvector available
but not pre-installed; the `vector` extension exists in
`pg_available_extensions` but not in `pg_extension`.

## Fix

The kiln does NOT install the extension itself (per AGENTS.md, no
schema-changing DDL outside Alembic). Run the install as a
one-shot from a psql session on the binding:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Then `cf restart cf-knowledge-kiln-api`. The boot preflight passes
and migrations apply on the first attempt.

## If the broker doesn't allow CREATE EXTENSION

Some hardened service plans block superuser DDL. File a ticket with
the broker team requesting pgvector pre-installed on the plan; the
kiln cannot ship without it.
