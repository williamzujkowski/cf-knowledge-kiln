# Data sources

Sources are **allowlisted**. The ingestion worker refuses to fetch any
source not listed in `config/sources.yaml`.

## Adding a source

1. Edit `config/sources.yaml` (copy from `config/sources.example.yaml`
   on first use).
2. Run `make ingest` locally to validate (Phase 3+).
3. Open a PR that includes:
   - the diff to `config/sources.yaml`,
   - a note about who owns the source and how often it should refresh,
   - confirmation that the source contains no secrets (`gitleaks`
     pre-commit will fail loudly if it does).

## Source schema

```yaml
sources:
  - name: <unique-slug>
    type: git                           # git | local | http (Phase 7+)
    repo: <owner>/<name>                # required for git
    branch: main
    include:
      - "docs/**/*.md"
    exclude:
      - "**/draft-*.md"
    status: active                      # active | inactive (skipped)
    authority: standard                 # standard | reference | informational
    default_owner: <team-or-username>
    default_sensitivity: internal       # public | internal | restricted
    last_reviewed_required: false
```

## Sensitivity

Set `default_sensitivity: restricted` on any source that may contain
information not meant for broad consumption. Restricted content is
filtered out of agent-facing responses by default and may be filtered
out of human responses based on the caller's identity (Phase 6+).

## Frontmatter

Individual documents may override source-level defaults via Markdown
frontmatter:

```yaml
---
title: "Incident Response Playbook"
status: active
owner: cybersecurity
last_reviewed: 2026-05-01
authority: standard
sensitivity: internal
control_ids: [IR-4, IR-5]
tags: [incident-response, playbook]
---
```

## Source registry vs. document registry

- `data_sources` table: tracks the *source* (the repo, the cadence,
  the last ingestion run, default metadata).
- `documents` table: tracks each *document* within a source, with
  per-doc metadata that may override defaults.
- `ingestion_runs` table: tracks each ingestion attempt with summary
  counts (files scanned, indexed, skipped, errors).

See [architecture.md](./architecture.md) and the migrations under
`src/cf_knowledge_kiln/db/migrations/` (Phase 2+) for the exact shapes.
