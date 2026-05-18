---
title: "ADR-0012: asyncpg connection pool size defaults to 8"
status: approved
owner: platform
doc_type: adr
sensitivity: internal
last_reviewed: 2026-04-22
tags: [adr, database, asyncpg, performance]
---

## ADR-0012: asyncpg connection pool size

## Status

Approved 2026-03-12.

## Context

The kiln runs as two Cloud Foundry apps (API + worker) sharing a single
Postgres binding. Both processes open asyncpg pools at startup. The
service plan caps total simultaneous connections at 25; pgvector
similarity queries hold a connection for the full RRF + ranking
pass (~250–500 ms under MockEmbedding, ~80–150 ms under a real
provider).

We measured the latency-per-pool-size curve on a 60 k chunk corpus:

| pool size | p50 search latency | p95 |
|---|---|---|
| 2 | 240 ms | 410 ms |
| 4 | 180 ms | 320 ms |
| 8 | 165 ms | 290 ms |
| 16 | 162 ms | 285 ms |

Past 8 the curve flattens; past 12 we start crowding the worker's pool.

## Decision

Default `KILN_PG_POOL_SIZE=8` for the API; `4` for the worker. Both
configurable via env, both bounded above by `25 - 2` (saved for
ad-hoc psql + migrations).

## Consequences

- The retrieval-quality eval runs with pool 8; reproduces production
  contention behavior.
- Operators sizing very-large corpora (≥ 500 k chunks) should raise
  the API pool to 12 and reduce worker concurrency to 2.
- The CF service plan must continue offering ≥ 25 connections; flagged
  in `deployment-cloud-foundry.md` as a deployment precondition.
