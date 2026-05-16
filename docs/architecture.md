# Architecture

cf-knowledge-kiln separates concerns into four layers. Each layer talks
to the one below it via narrow interfaces; **no layer reaches around**.
The single most load-bearing rule: the API and ingestion layers do not
own retrieval logic.

## Layer diagram

```text
┌────────────────────────────────────────────────────────────┐
│ Experience Layer                                           │
│   src/cf_knowledge_kiln/api/                               │
│   - FastAPI app, routers, middleware                       │
│   - Two response shapes: human (UI cards) + agent (packs)  │
│   - Auth, rate limiting, request/response logging          │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│ Retrieval Orchestration                                    │
│   src/cf_knowledge_kiln/retrieval/                         │
│   - Query normalization                                    │
│   - Metadata filtering                                     │
│   - Hybrid retrieval: pgvector similarity + Postgres FTS   │
│   - Rank fusion (reciprocal-rank / weighted)               │
│   - Authority / freshness / status weighting               │
│   - Conflict and stale-source detection                    │
│   - Context-pack assembly + token budgeting                │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│ Knowledge Index                                            │
│   src/cf_knowledge_kiln/db/                                │
│   - Postgres + pgvector (single DB)                        │
│   - documents, document_chunks, chunk_embeddings,          │
│     rag_queries, rag_feedback, ingestion_runs,             │
│     data_sources, model_registry, context_packs            │
│     (9 tables; chunk_embeddings.dimensions per-row)        │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│ Ingestion                                                  │
│   src/cf_knowledge_kiln/ingestion/                         │
│   - Source connectors (allowlisted only)                   │
│   - Markdown + frontmatter parsing                         │
│   - Structure-aware chunking                               │
│   - Embedding via provider abstraction                     │
│   - Provenance tracking (repo, path, commit, heading path) │
└────────────────────────────────────────────────────────────┘
```

## Process model on Cloud Foundry

Two CF apps, both bound to the same Postgres service:

- `cf-knowledge-kiln-api` — HTTP, routed, HTTP health check.
- `cf-knowledge-kiln-worker` — `no-route: true`, process health check.

See [ADR-0004](./adr/0004-cf-process-model.md) for rationale.

## Retrieval flow

```text
client request
  │
  ▼
api/ ─── normalize → orchestrator
                       │
                       ├── apply metadata filters (status, authority,
                       │      owner, freshness, control_id, tags, ...)
                       ├── pgvector similarity  ──┐
                       ├── Postgres FTS scoring  ──┤ merge (rank fusion)
                       │                           │
                       ├── authority/freshness/status weighting
                       ├── conflict detection (same heading_path,
                       │      conflicting active sources)
                       ├── token budgeting (agent only)
                       │
                       ▼
                  shape the response (human OR agent serializer)
                       │
                       ▼
                  return; audit-log the query
```

Hybrid retrieval is MVP per [ADR-0002](./adr/0002-postgres-pgvector.md)
(reaffirmed by [ADR-0008](./adr/0008-pgvector-mvp-critical.md)). Bound
Postgres must have the `vector` extension enabled — the
cf-local-service-broker `pgvector` plan handles `CREATE EXTENSION` at
provision time when the broker is pointed at a pgvector-capable
backing Postgres (e.g. via [bosh-pgvector-release](https://github.com/williamzujkowski/bosh-pgvector-release)).

## Embedding index strategy (per-dimension HNSW)

`chunk_embeddings.embedding` is an unconstrained `vector` column so the
table can hold embeddings from multiple models simultaneously, with
per-row dimensions recorded in `chunk_embeddings.dimensions`. HNSW
indexes, however, require a fixed dimension. The strategy is:

- **One HNSW partial index per registered model dimension**, keyed by
  the predicate `WHERE dimensions = N` and the expression
  `(embedding::vector(N)) vector_cosine_ops`. The initial migration
  creates the `_768` index for the default model
  (`nomic-embed-text-v1.5`); operators add `_<N>` indexes in follow-up
  migrations when they enable a non-768 model in `model_registry`.

- **Retrieval queries always include `WHERE dimensions = N` and a cast
  `embedding::vector(N) <=> $query`** so the planner can match the
  partial index. The repository layer (in `db/repositories/`) is the
  natural place to enforce this contract; retrieval (Phase 5) will
  call into it with the active model's dimensions from settings.

- **The index name is a contract.** Names follow
  `ix_chunk_embeddings_hnsw_<dim>` so the schema is self-documenting and
  cross-references the active model's dimension. Renaming this scheme
  requires updating the operator runbook for adding a new model.

- **Sequential scan is the fallback** for dimensions without a matching
  partial index (e.g. during a re-embedding migration when both the
  old and new model coexist transiently). This is acceptable for
  small temporary windows; long-lived multi-model deployments must
  carry an HNSW per active dimension.

The HNSW index is empty until embeddings are ingested (Phase 3/4), so
the cost of creating it in the initial migration is zero. The benefit
is that the schema's contract is visible from day one.

## Anti-patterns we are deliberately avoiding

These are inherited from the plan and enforced by review:

- Putting model weights in the app repo.
- Building a chatbot before retrieval quality exists.
- Making the LLM responsible for access control.
- Giving agents raw SQL or DB access.
- Storing only embeddings without source metadata.
- Treating draft/deprecated docs the same as approved docs in default retrieval.
- Indexing everything; no source allowlist.
- Returning uncited answers.
- Assuming one embedding dimension forever (dimensions are per-row in `chunk_embeddings`).
- Assuming one model provider forever.
- Making humans and agents consume the same response shape.

## What lives where (Phase 1 baseline)

| Path                                              | Purpose                                                |
| ------------------------------------------------- | ------------------------------------------------------ |
| `src/cf_knowledge_kiln/api/app.py`                | FastAPI factory + router wiring                        |
| `src/cf_knowledge_kiln/api/health.py`             | `/healthz`, `/readyz`, `/version`                      |
| `src/cf_knowledge_kiln/api/cli.py`                | `cf-knowledge-kiln serve` entrypoint                   |
| `src/cf_knowledge_kiln/config/settings.py`        | Pydantic settings (env-first)                          |
| `src/cf_knowledge_kiln/retrieval/`                | Empty in Phase 1; Phase 5 lands implementation         |
| `src/cf_knowledge_kiln/ingestion/`                | Empty in Phase 1; Phase 3 lands implementation         |
| `src/cf_knowledge_kiln/db/`                       | Empty in Phase 1; Phase 2 lands the 9-table schema + `CREATE EXTENSION vector` |
| `openapi/openapi.yaml`                            | Hand-authored OpenAPI 3.1 contract                     |
| `manifest.yml`, `Procfile`, `scripts/start-*.sh`  | Cloud Foundry deployment                               |
| `config/*.example.yaml`                           | Model / source / security config templates             |
