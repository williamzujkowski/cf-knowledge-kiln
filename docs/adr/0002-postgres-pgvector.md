---
id: ADR-0002
title: Postgres + pgvector as the vector store
status: accepted
date: 2026-05-16
deciders: william
superseded_by: null
---

> **History note.** This ADR was briefly marked superseded by ADR-0007
> (FTS-first), then reinstated by [ADR-0008](./0008-pgvector-mvp-critical.md)
> on the same day after owner clarification that embeddings are
> MVP-critical and the BOSH release shipped, dropping the infrastructure
> cost. The reasoning below is the active retrieval-store decision again.

## Context

We need vector + keyword (FTS) search, transactional consistency
between chunks and embeddings, and an honest path to Cloud Foundry
deployment. The plan calls for `Postgres + pgvector` explicitly.
Discovery confirmed `cf-local-service-broker` already provides
PostgreSQL via OSBAPI v2 — the binding pattern exists upstream.

## Decision

Single Postgres database for all data: `documents`, `document_chunks`,
`chunk_embeddings`, `rag_queries`, `rag_feedback`, `ingestion_runs`,
`data_sources`, `model_registry`, `context_packs`. The `pgvector`
extension stores embeddings; `tsvector` indexes (Postgres-native FTS)
provide keyword retrieval. We will not introduce a separate vector
store, search engine, or message bus for the MVP.

Embedding dimensionality is stored per-row, not assumed globally —
we will change embedding models over the system's lifetime.

## Consequences

- One database, one migration tool (Alembic), one transactional
  boundary. Simpler operations.
- Hybrid retrieval lives entirely in SQL + Python. No Elastic /
  OpenSearch / Weaviate / Qdrant operational burden.
- pgvector scales to ~10M vectors comfortably on a single CF-managed
  Postgres. If we exceed that, that's a good problem; we can introduce
  a dedicated vector store *with measurements*.
- We get RDS-compatible deployment via CF service brokers.

## Alternatives considered

- **Standalone vector store (Qdrant, Weaviate, Milvus)** — better
  pure-vector performance, worse operational footprint, fragments the
  source-of-truth across two systems. Rejected for the MVP.
- **Elasticsearch / OpenSearch** — excellent FTS, weaker vector story,
  heavy. Rejected.
- **DuckDB + VSS** — fun, but no CF service binding pattern. Rejected.
