# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 0: Discovery report (homelab-iac CF patterns, pre-commit baseline, ADR conventions).
- Phase 1: Repo scaffold — README, AGENTS.md, SECURITY.md, CONTRIBUTING.md, Makefile, pyproject.toml, pre-commit config.
- Phase 1: FastAPI skeleton with `/healthz`, `/readyz`, `/version` endpoints.
- Phase 1: Settings/config loader with env-var precedence.
- Phase 1: OpenAPI 3.1 skeleton covering planned human + agent endpoints.
- Phase 1: Cloud Foundry manifest, Procfile, and start scripts (two-app split: API + worker).
- Phase 1: ADRs 0001–0005 documenting initial architectural decisions.
- Phase 1: GitHub Actions CI for lint + typecheck + test + openapi-lint.

### Reverted (same day)

- ADR-0007 (FTS-first, embeddings-deferred) was authored and then superseded by ADR-0008 within the same day after owner clarification that kiln ships as a pgvector-backed RAG CF app from MVP. The deployment cost that motivated the deferral also dropped to operator-runbook level when [bosh-pgvector-release](https://github.com/williamzujkowski/bosh-pgvector-release) shipped. ADR-0002 (Postgres + pgvector) is the active retrieval-store decision again.

### Changed (post-reversal)

- pgvector restored to the `db` extra in `pyproject.toml`; `embeddings` extra removed.
- `config/models.example.yaml`: embedding model `enabled: true` again.
- `manifest.yml` + `docs/deployment-cloud-foundry.md` recommend the `cf-local-service-broker pgvector` plan against a `bosh-pgvector-release` Postgres VM. Local dev uses `pgvector/pgvector:pg16` Docker image.
- Phase 2 schema returns to 9 tables (`chunk_embeddings` + `model_registry` re-included).
- Phase 4 epic un-deferred; Phase 5.5 decision issue closed as "decided early".
- Deploy gate on the homelab CF blocks on [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3) until the BOSH release is operator-deployed.

[Unreleased]: https://github.com/williamzujkowski/cf-knowledge-kiln/commits/main
