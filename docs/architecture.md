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
│   - Hybrid retrieval (vector + FTS) + merge                │
│   - Ranking + authority/freshness weighting                │
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
                       ├── apply metadata filters
                       ├── vector search    ──┐
                       ├── FTS search       ──┤ merge + rank
                       │                      │
                       ├── authority/freshness weighting
                       ├── conflict detection
                       ├── token budgeting (agent only)
                       │
                       ▼
                  shape the response (human OR agent serializer)
                       │
                       ▼
                  return; audit-log the query
```

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
- Relying on vector search alone.
- Assuming one embedding dimension forever.
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
| `src/cf_knowledge_kiln/db/`                       | Empty in Phase 1; Phase 2 lands implementation         |
| `openapi/openapi.yaml`                            | Hand-authored OpenAPI 3.1 contract                     |
| `manifest.yml`, `Procfile`, `scripts/start-*.sh`  | Cloud Foundry deployment                               |
| `config/*.example.yaml`                           | Model / source / security config templates             |
