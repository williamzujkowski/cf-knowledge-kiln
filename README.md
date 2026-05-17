# cf-knowledge-kiln

> Cloud Foundry-ready RAG knowledge substrate. Hybrid search over your internal docs, cited answers for humans, bounded context packs for agents.

**Status:** Phases 0–8 complete: scaffold, Postgres+pgvector data layer, ingestion pipeline (Git/HTTP/Local sources with SSRF guard + DNS pinning), embedding providers, hybrid retrieval engine, `/v1/search` + `/v1/agent/context-pack` JSON APIs, server-rendered HTMX search UI with feedback, bearer-token auth middleware, per-IP rate limiting, SBOM + grype scan in CI. Phase 9 (eval harness, forking guide) is in progress — the retrieval eval is live (`make eval`). See [HANDOFF.md](./HANDOFF.md) for the live status, [plans/cf-rag-plan.md](./plans/cf-rag-plan.md) for the full implementation plan, and [docs/INDEX.md](./docs/INDEX.md) for documentation entry points.

## What this is

A reusable, forkable knowledge app for Cloud Foundry teams that need:

- hybrid retrieval (pgvector + Postgres FTS) + rich metadata filtering across internal documentation
- **cited** retrieval for humans (no uncited answers)
- structured **context packs** for AI agents (bounded token budget, structured evidence, explicit uncertainty)
- Postgres + pgvector for retrieval (see [ADR-0002](./docs/adr/0002-postgres-pgvector.md) / [ADR-0008](./docs/adr/0008-pgvector-mvp-critical.md))
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

See [HANDOFF.md](./HANDOFF.md) for the "how to start working" walkthrough (env vars, pgvector container, where each phase's code lives), and [docs/deployment-cloud-foundry.md](./docs/deployment-cloud-foundry.md) for CF deployment.

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

## Fork & repurpose

This repo is designed to be forked by another team and adapted to their corpus + Cloud Foundry foundation. The minimum path:

1. **Fork and rename.** Update every place this repo's GitHub URL is hard-coded:
   - `AGENTS.md` — `Repository:` and `Owner:` fields at the top.
   - `pyproject.toml` — `[project.urls]` (`Homepage`, `Issues`, `Repository`).
   - `manifest.yml` — app name, route, memory quota.
   - `.github/CODEOWNERS` — replace `@williamzujkowski` with your team handle so PR reviews route correctly.
   - `SECURITY.md` and `CODE_OF_CONDUCT.md` — disclosure URLs point at this repo's `/security/advisories/new`.
   - `openapi/openapi.yaml` — `info.contact` / issue-tracker URL.
   - `HANDOFF.md` — operational state, written by the upstream maintainer; treat as starting context for your fork and rewrite as your work diverges.
2. **Configure sources.** Copy `config/sources.example.yaml` → `config/sources.yaml` and list the repos / HTTP roots you want indexed. Sources not in the allowlist are refused — by design.
3. **Configure models.** Copy `config/models.example.yaml` → `config/models.yaml` and pick an embedding provider. The default is a local sentence-transformers model so the dev loop has zero external dependencies; swap to an OpenAI-compatible endpoint when you want a real provider. Adversary-origin model weights are blocked (`docs/model-providers.md`).
4. **Provision Postgres + pgvector.** In Cloud Foundry, bind a Postgres service from a broker that exposes `pgvector` (`KILN_PG_SERVICE_NAME` selects which binding). Locally, the integration tests assume `pgvector/pgvector:pg16` at `localhost:5432`; see `tests/integration/conftest.py` for the default DSN.
5. **Push.** `make cf-push` deploys the two-process layout from `manifest.yml` (`cf-knowledge-kiln-api` + `cf-knowledge-kiln-worker`). `make ingest` enqueues an initial corpus build.
6. **Verify.** `scripts/smoke-test.sh` posts a query to the deployed API; `make eval` runs the retrieval-quality harness against your seeded corpus.

This README walks the happy path. The expanded forking guide (deeper troubleshooting, CF-foundation variations, ingest tuning) lands as part of Phase 9 (#32).

## License

MIT. See [LICENSE](./LICENSE).

## For AI coding agents

Read [AGENTS.md](./AGENTS.md) before making changes. Claude Code users: [CLAUDE.md](./CLAUDE.md) symlinks to AGENTS.md.
