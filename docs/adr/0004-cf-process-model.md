---
id: ADR-0004
title: Cloud Foundry process model — separate API and worker apps
status: accepted
date: 2026-05-16
deciders: william
---

## Context

Ingestion (large reads, embedding API calls, periodic batch work) and
request serving (low-latency HTTP) have different runtime profiles.
Co-locating them risks ingestion saturating the request path and
makes per-process tuning (memory, concurrency, instance count)
impossible.

## Decision

Two CF apps from a single source tree:

- `cf-knowledge-kiln-api` — HTTP-facing, routes attached, HTTP health
  check on `/healthz`, 1 GiB memory baseline.
- `cf-knowledge-kiln-worker` — `no-route: true`, process health check,
  2 GiB disk for source clones, 1 GiB memory baseline.

Both bind the same Postgres service. The UI (Phase 6) is served by
the API app for the MVP; if a separate `cf-knowledge-kiln-ui` makes
sense later we split it.

## Consequences

- We can scale instances independently.
- Worker restarts do not interrupt the request path.
- We pay slightly more memory (two copies of the package).
- The MVP needs a way for the API to enqueue work for the worker.
  Phase 3 will pick between a Postgres-backed job queue (simplest,
  one fewer service) and a managed queue. We default to Postgres
  until we hit a documented limit.

## Alternatives considered

- **Single CF app, async background tasks** — simpler, but mixes
  workload profiles and prevents independent scaling.
- **Three apps (api + worker + ui)** — premature for an MVP that
  doesn't yet have a UI.
