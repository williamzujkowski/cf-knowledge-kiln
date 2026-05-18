---
title: "Auth policy: bearer-token rotation cadence"
status: active
owner: platform
doc_type: standard
sensitivity: internal
last_reviewed: 2026-01-08
tags: [auth, security, policy]
---

## Bearer token rotation policy

Operators with kiln API access rotate their bearer tokens every
**90 days**. Generation is via `cf set-env` against the
`KILN_AUTH_TOKENS` slot; rotation overwrites the previous value and
the API picks up the new token on the next process restart.

## Cadence rationale

The 90-day window matches the platform-wide credential policy
documented in the internal security standard. Shortening rotation
without coordinating with the broader operator population creates a
window where a stale `cf env` snapshot still works while a rotated
token does not — a real on-call paged us for exactly this in
2025-Q3.

## Mechanics

1. Generate a new token: `python -m cf_knowledge_kiln.auth.tokens new`.
2. `cf set-env cf-knowledge-kiln-api KILN_AUTH_TOKENS '<new-token>'`.
3. `cf restart cf-knowledge-kiln-api`.
4. Distribute the new token to authorized operators via the secrets
   channel; revoke the prior token on the next rotation cycle.

## Exception

A token compromise rotates immediately, not on the 90-day cadence.
File an incident, generate the new token, restart, and document in
`#sec-incidents`.
