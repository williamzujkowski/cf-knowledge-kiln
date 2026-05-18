---
title: "Auth policy: bearer-token rotation cadence (revised)"
status: active
owner: platform
doc_type: standard
sensitivity: internal
last_reviewed: 2026-05-12
tags: [auth, security, policy]
---

## Bearer token rotation policy

Operators with kiln API access rotate their bearer tokens every
**30 days**. The cadence tightened after the 2026-Q1 security review;
the prior 90-day window is preserved at `auth-policy-legacy.md` for
audit reference and should NOT be followed.

## Cadence rationale

The 30-day window aligns the kiln with the rotation cadence of every
other internal service that consumes the same operator population.
A shorter window also bounds the blast radius of a leaked `cf env`
snapshot to roughly one operations cycle.

## Mechanics

1. Generate a new token: `python -m cf_knowledge_kiln.auth.tokens new`.
2. `cf set-env cf-knowledge-kiln-api KILN_AUTH_TOKENS '<new-token>'`.
3. `cf restart cf-knowledge-kiln-api`.
4. Distribute the new token via the secrets channel; revoke the prior
   token on the next rotation cycle.

## Exception

A token compromise rotates immediately, not on the 30-day cadence.
File an incident, generate the new token, restart, and document in
`#sec-incidents`.
