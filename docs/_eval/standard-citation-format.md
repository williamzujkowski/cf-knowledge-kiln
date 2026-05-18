---
title: "Standard: kiln citation format"
status: approved
owner: platform
doc_type: standard
sensitivity: internal
last_reviewed: 2026-05-01
tags: [standard, citation, retrieval, contract]
---

## Standard: kiln citation format

Every chunk returned by `/v1/search` or surfaced in an agent context
pack carries a citation. The citation has exactly four parts:

1. **`repo`** — stable label for the source repository. Configured in
   `sources.yaml`; never derived from URL.
2. **`path`** — POSIX path relative to the repo root.
3. **`heading_path`** — list of headings from H1 down to the chunk's
   nearest ancestor. Empty list means "document-anywhere" (no
   heading granularity).
4. **`commit_sha`** — the git commit at ingest time. Present for git
   sources; `null` for local-filesystem sources where the SHA isn't
   knowable.

## Why all four

Each piece survives a different kind of churn:

- `repo` + `path` survive file moves within the repo only via the
  ingest pipeline's path-rewrite step (it doesn't exist yet — see
  `discovery-report.md`).
- `heading_path` survives chunk-id churn (chunk IDs change every
  reingest; headings change only on edit).
- `commit_sha` is the time anchor for "what version of the doc said
  this?" — load-bearing for the deprecated-doc warning.

## What citations are NOT

- They are NOT URLs. The UI may render a URL via the `source_url`
  field, which is a presentation concern. Agents must use the
  four-part citation for any persistence.
- They do NOT include line numbers. Markdown chunk boundaries drift;
  any line-number-bearing citation will go stale within one reingest.

## Cited-or-silent

Per `AGENTS.md` prime directive: every retrieval response cites a
source. Never fabricate provenance. A pack with `requires_human_review
= true` and an empty chunk list still carries the empty citation
shape, never substituted text.
