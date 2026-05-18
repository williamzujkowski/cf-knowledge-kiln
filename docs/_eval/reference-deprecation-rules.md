---
title: "Reference: document status enum semantics"
status: active
owner: platform
doc_type: reference
sensitivity: internal
last_reviewed: 2026-05-04
tags: [reference, status, semantics, retrieval]
---

## Reference: status enum semantics

Every kiln chunk carries a status drawn from a fixed enum. The
retrieval engine, the result-card UI, and the agent context-pack
serializer all derive their behavior from this value; the enum is
hard-coded, not configurable.

## The six values

- **`active`** — current, periodically reviewed, authoritative. The
  default-lane status.
- **`approved`** — active AND passed a formal review gate. Most ADRs
  and standards live here. Treated identically to active for
  retrieval ranking; the distinction is editorial.
- **`draft`** — work-in-progress. Queryable on demand only;
  contradicts active docs frequently.
- **`deprecated`** — replaced by a current doc. Still queryable for
  archaeology; the result card dims the title and a
  `deprecated_source` warning fires. The agent pack short-circuits
  to `requires_human_review = true` when every retrieved chunk is
  deprecated.
- **`archived`** — historical interest only, no current replacement.
  Same retrieval treatment as deprecated.
- **`superseded`** — replaced by a specific newer version (the
  citation graph points to the replacement). Same retrieval
  treatment as deprecated.

## Per AGENTS.md

> Deprecated docs must be visibly flagged in results. Never silently
> return stale guidance as current truth.

The flag is the colored status badge on the result card AND the
warning row above the result list AND the agent-pack warning array.
All three surfaces fire from the same status field — never split.

## Status transitions

A status change is a documentation event with its own audit trail.
The ingest pipeline preserves the previous status in the
`status_history` JSONB column; the eval suite uses that field to
confirm a doc was once `active` before going `deprecated`, which
calibrates the staleness threshold.
