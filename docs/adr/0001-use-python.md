---
id: ADR-0001
title: Use Python for the MVP
status: accepted
date: 2026-05-16
deciders: william
supersedes: null
superseded_by: null
---

## Context

The plan (`plans/cf-rag-plan.md`) directs us to pick Python or TypeScript
*after* repo exploration. Both were viable on paper. Discovery found:

- `homelab-iac` has no FastAPI / Express / similar app — no pre-existing
  language commitment to honor.
- The Cloud Foundry example manifest in the plan already targets
  `python_buildpack`.
- The RAG and embeddings ecosystem (FastAPI + pgvector + asyncpg +
  sentence-transformers + `python-frontmatter` + `mistune`) is
  Python-native. The TypeScript equivalents (e.g., `@lancedb/lancedb`,
  `transformers.js`) exist but lag behind, especially for local
  embedding models.
- Pre-commit baseline of `ruff` + `mypy --strict` + `pytest` gives us
  the same correctness floor we already enforce on the TypeScript side
  of nexus-agents.
- The plan's example pre-commit hooks call out `ruff`, `pytest`,
  `mypy`, and `bandit` directly.

## Decision

The MVP is **Python 3.12** with FastAPI + Pydantic v2 + asyncpg +
SQLAlchemy + Alembic + pgvector. Strict typing enforced via
`mypy --strict` and `ruff` configured against the same rule families
nexus-agents uses on TypeScript.

## Consequences

- We fight the ecosystem less on embeddings, retrieval, and ingestion.
- `homelab-iac` does not get to dogfood a TypeScript CF deployment
  pattern from this repo; that is fine — `homelab-agent` already plays
  that role.
- Two new repos in this user's tree are Python (this one and a few
  evaluation harnesses); we adopt their conventions where reasonable.
- We commit to `make verify` running `ruff` + `mypy` + `pytest` +
  `openapi-lint` as the single local gate.

## Alternatives considered

- **TypeScript + Fastify/Hono** — aligns with `nexus-agents`. Rejected:
  weaker embeddings / vector-store ecosystem, and the user's CF apps
  are already polyglot.
- **Go** — strong CF buildpack story, but the RAG ecosystem is weak;
  rejected.
- **Rust** — premature optimization for an MVP retrieval substrate.
