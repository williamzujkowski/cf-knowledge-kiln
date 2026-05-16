# cf-knowledge-kiln

> Cloud Foundry-ready RAG knowledge substrate. Hybrid search over your internal docs, cited answers for humans, bounded context packs for agents.

**Status:** Phase 1 skeleton. Not yet production-ready. See [plans/cf-rag-plan.md](./plans/cf-rag-plan.md) for the full implementation plan and [docs/INDEX.md](./docs/INDEX.md) for documentation entry points.

## What this is

A reusable, forkable knowledge app for Cloud Foundry teams that need:

- semantic + keyword search across internal documentation
- **cited** retrieval for humans (no uncited answers)
- structured **context packs** for AI agents (bounded token budget, structured evidence, explicit uncertainty)
- Postgres + pgvector for retrieval
- OpenAPI-documented HTTP API
- secure-by-default Cloud Foundry deployment (manifest, Procfile, health checks, service bindings)

This is not a chatbot. It is a **trusted knowledge substrate** with two first-class users: humans (search-first UI) and AI agents (deterministic, cited context-pack API).

## Two journeys, one retrieval backend

```text
Humans ─────► /v1/search        ──┐
                                  ├──► hybrid retrieval (pgvector + FTS)
Agents ─────► /v1/agent/*        ──┘            │
                                                ▼
                                  Postgres + pgvector
```

Same retrieval substrate. Different response shapes: humans get UI-friendly result cards with previews and feedback; agents get bounded context packs with token budgets, warnings, and `requires_human_review` flags.

## Quick start (development)

```bash
make bootstrap        # install dev deps
make install          # install package
make migrate          # apply DB migrations (Phase 2+)
make ingest           # ingest fixture docs (Phase 3+)
make run              # start API on :8080
make verify           # the local quality gate (lint + typecheck + test + openapi-lint)
```

See [docs/getting-started.md](./docs/getting-started.md) once Phase 1 lands, and [docs/deployment-cloud-foundry.md](./docs/deployment-cloud-foundry.md) for CF deployment.

## Architecture

Four layers, kept apart on purpose. See [docs/architecture.md](./docs/architecture.md).

```text
┌──────────────────────────────────────┐
│ Experience Layer (UI / API / CLI)    │
├──────────────────────────────────────┤
│ Retrieval Orchestration              │
├──────────────────────────────────────┤
│ Knowledge Index (Postgres+pgvector)  │
├──────────────────────────────────────┤
│ Ingestion (sources, chunking, embed) │
└──────────────────────────────────────┘
```

UI, agent API, and ingestion do **not** own retrieval logic. Retrieval is centralized.

## License

MIT. See [LICENSE](./LICENSE).

## For AI coding agents

Read [AGENTS.md](./AGENTS.md) before making changes. Claude Code users: [CLAUDE.md](./CLAUDE.md) symlinks to AGENTS.md.
