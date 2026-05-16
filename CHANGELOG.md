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
- ADR-0007: FTS-first retrieval; embeddings deferred to a Phase 5.5 decision gated on the Phase 9 eval harness. Supersedes ADR-0002.

### Changed

- Architecture / deployment docs / manifest retargeted for FTS-only MVP. Phase 2 schema shrinks from 9 tables to 7 (drops `chunk_embeddings`, `model_registry`).
- `pyproject.toml`: `pgvector` moved out of the `db` extra into a new `embeddings` extra so the MVP install footprint stays minimal.
- `config/models.example.yaml`: embedding model marked disabled until Phase 5.5.

[Unreleased]: https://github.com/williamzujkowski/cf-knowledge-kiln/commits/main
