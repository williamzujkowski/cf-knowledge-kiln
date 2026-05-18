---
title: "Standard: default status preference"
status: approved
owner: platform
doc_type: standard
sensitivity: internal
last_reviewed: 2026-04-18
tags: [standard, status, retrieval, defaults]
---

## Standard: default status preference

The kiln defaults to surfacing `active` and `approved` chunks; every
other status is opt-in via the UI status pills or
`filters.status` in the JSON API.

The default is configurable via the env var
`KILN_DEFAULT_STATUS_PREFERENCE` (comma-separated). The runtime
parses it once at boot in `config/settings.py` and the search route
falls back to it whenever the caller doesn't supply explicit
filters.

## Why this default

- **Active**: the document is current, periodically reviewed, and
  authoritative.
- **Approved**: an active document that has additionally passed a
  formal review gate (ADRs, standards).
- Everything else (`draft`, `deprecated`, `archived`, `superseded`)
  has a reason to be visible only when the user explicitly asks for
  it. Surfacing them by default would normalize stale guidance.

## Deprecated docs are NOT the default

A deprecated chunk still appears in search results when the user
toggles the `deprecated` pill — the deprecation warning fires, the
card is dimmed, and the agent-side `requires_human_review` trips.
But the default search NEVER returns deprecated chunks; that's the
hard contract.

## Why not also `draft`

Drafts are work-in-progress. They're queryable on demand (operators
debugging an in-flight standard) but they shouldn't surface in the
default search lane because draft text routinely contradicts the
active document on the same topic, and the default lane is the
read-many surface that engineers trust.
