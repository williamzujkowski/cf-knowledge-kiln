---
id: ADR-0003
title: OpenAPI 3.1 + separate human and agent response shapes
status: accepted
date: 2026-05-16
deciders: william
---

## Context

Humans and agents need the same retrieval substrate but different
response shapes. Humans want previews, freshness badges, and feedback
links; agents want token budgets, structured warnings, and a
`requires_human_review` flag. The plan is explicit that "making humans
and agents consume the exact same response shape" is an architectural
anti-pattern.

## Decision

- Single OpenAPI 3.1 contract at `openapi/openapi.yaml`. The contract
  is authored by hand (not auto-generated from FastAPI) so it remains
  stable across implementation changes.
- Two route trees share one retrieval backend:
  - `/v1/search`, `/v1/answer`, `/v1/documents/*` — human shapes.
  - `/v1/agent/search`, `/v1/agent/context-pack`, `/v1/agent/answer`,
    `/v1/agent/sources/resolve`, `/v1/agent/feedback` — agent shapes.
- Agent shapes always include `warnings`, `token_budget`, and
  `requires_human_review`. Human shapes may include warnings but
  never `requires_human_review`.
- `make openapi-lint` is part of `make verify`. The CI gate blocks
  drift between spec and implementation by comparing the FastAPI-
  generated spec against the hand-authored one (Phase 5+ test).

## Consequences

- One contract, no client-server drift.
- Adding a new shape is a contract-first change, not an implementation-
  first one.
- We pay the cost of maintaining the spec by hand; we accept that cost
  in exchange for stability.
- The human + agent split forces us to keep response-shaping code out
  of the retrieval layer.

## Alternatives considered

- **Auto-generate from FastAPI** — keeps the spec in sync trivially
  but makes contract changes implicit. Rejected for an API we want
  third parties to fork.
- **One shape for everyone** — simpler, but mixes UX-driven fields
  with agent-decision fields, hurting both. Explicitly rejected by
  the plan.
