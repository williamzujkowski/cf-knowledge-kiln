# Documentation index

This is the starting point for all cf-knowledge-kiln documentation.

## Read-first

- [README.md](../README.md) — what this is and how to start.
- [AGENTS.md](../AGENTS.md) — instructions for AI coding agents (also linked as `CLAUDE.md`).
- [SECURITY.md](../SECURITY.md) — security policy and threat-model summary.

## Architecture and design

- [architecture.md](./architecture.md) — four-layer architecture, retrieval flow, anti-patterns.
- [user-journeys.md](./user-journeys.md) — human and agent user journeys side-by-side.
- [adr/README.md](./adr/README.md) — architectural decision records (ADRs 0001–0005).

## Operations

- [deployment-cloud-foundry.md](./deployment-cloud-foundry.md) — `cf push` walkthrough, service bindings, env vars.
- [configuration.md](./configuration.md) — settings reference (env vars + YAML files).
- [model-providers.md](./model-providers.md) — model registry, provenance, allowlist.
- [data-sources.md](./data-sources.md) — adding a new ingestion source.
- [security.md](./security.md) — threat model, untrusted-input handling, prompt-injection notes.

## Plan and discovery

- [../plans/cf-rag-plan.md](../plans/cf-rag-plan.md) — original implementation plan.
- [discovery-report.md](./discovery-report.md) — Phase 0 discovery findings.

## Conventions

- ADR format: plain Markdown + YAML frontmatter. Status values: `proposed | accepted | rejected | superseded`.
- Docs in `docs/` use Markdown headings starting at `#` (one H1 per file).
- Code references use the `file:line` form so editors can jump to them.
- All file paths in docs are relative to the repo root.
