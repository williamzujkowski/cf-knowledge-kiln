---
id: ADR-0011
title: API compatibility and schema evolution policy
status: accepted
date: 2026-05-26
deciders: william
supersedes: null
superseded_by: null
---

## Context

PRs [#266](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/266) (ErrorResponse envelope), [#299](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/299) (OpenAPI bearer security + per-status error wiring), and [#305](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/305) (`untrusted_content_notice_id`) shipped a typed API contract for agent consumers. Multiple PRs referenced "the API compatibility policy" — a doc that didn't yet exist.

The agent-API audit (post-#292) flagged two concrete gaps:

1. **No declared versioning beyond `/v1/`.** `info.version` is `0.1.0` and the path prefix is `/v1/`. No doc says how a field addition vs removal is announced or signaled.
2. **`extra=forbid` round-trip hazard.** Every Pydantic response model uses `ConfigDict(extra="forbid")`. Codegen consumers that **re-serialize** the response (e.g. forward it through their own typed model and back) cannot tolerate the server adding a new field — the round-trip fails. Adding a field is therefore not as additive as it would be for a more permissive contract.

This ADR ratifies the rules the project will hold itself to.

## Decision

### Compatibility tiers

| Change | Status | Mechanism | Example |
|---|---|---|---|
| Add a NEW optional response field | **Additive (minor bump)** | Document in PR description as "API additive"; OpenAPI gets the new field with a clear `description` | Add `untrusted_content_notice_id` |
| Add a NEW required response field | **Breaking (major bump)** | Same as removing a field — see below | Would have been the case for `notice_id` if we'd shipped without back-compat machinery |
| Add a NEW request field, optional | **Additive (minor bump)** | OpenAPI gets the field with `nullable: true` or a default | Add a new optional filter |
| Add a NEW request field, required | **Breaking (major bump)** | Plan a 2-step migration: ship as optional with a default for one major; require in the next | — |
| Add a NEW operation (path + method) | **Additive (minor bump)** | Document in the agent guide | Plan an alongside operation |
| Remove a response field | **Breaking (major bump)** | Pre-announce with `deprecated: true` in OpenAPI + `Deprecation` HTTP header for ≥ 1 minor before removal | Remove `legacy_x` field |
| Rename a response field | **Breaking (major bump)** | Same as remove + add. Both fields ship side-by-side for ≥ 1 minor; the old emits a `Deprecation` header | — |
| Add a NEW `error_code` enum value | **Additive (minor bump)** | Document in the PR + `docs/error-codes.md`. Consumers with default-branched switches keep working; exhaustive `match` consumers must update — flagged in the PR description so they can grep their releases. The drift test in [`tests/unit/test_openapi_security_and_errors.py`](../../tests/unit/test_openapi_security_and_errors.py) enforces enum-spec sync. | Add a future `quota_exhausted` code |
| Remove an `error_code` enum value | **Breaking (major bump)** | Same as remove field. Pre-announce ≥ 1 minor; emit `Deprecation` header on responses that used the code | — |
| Tighten validation (e.g., narrow a regex, add a `pattern`) | **Breaking (major bump)** | Pre-announce as `deprecated: true` on the property for ≥ 1 minor with a `description` callout | — |
| Loosen validation (widen a regex, drop a `pattern`) | **Additive (minor bump)** | Document; no consumer-facing change for well-formed inputs | — |
| Change `untrusted_content_notice` prose | **Additive** when the meaning is unchanged; **breaking** when meaning changes | The `untrusted_content_notice_id` (#305) bumps to `vN+1` only on the meaning-changing edit. Consumers switch on the id; the prose is the human-readable rendering | — |

### Mechanism: `Deprecation` + `Sunset` headers

When a field, code, or operation is marked breaking and pre-announced:

1. The OpenAPI property / operation gets `deprecated: true` in the same release where the announcement starts.
2. Responses that include the deprecated field carry an HTTP `Deprecation` header (per [RFC 8594](https://datatracker.ietf.org/doc/rfc8594/)) with the ISO-8601 deprecation date.
3. Responses also carry a `Sunset` header (per RFC 8594) with the ISO date the field will be removed — at minimum the start of the next major (`/v2/`).
4. The agent guide ([docs/agent-integration-guide.md](../agent-integration-guide.md)) is updated with a "Deprecations" section listing every active `Deprecation`/`Sunset` pair, the migration path, and the deadline.

### Mechanism: `/v1/` path prefix

The path prefix `/v1/` represents the major version. Breaking changes that affect every consumer (e.g., the response envelope shape, the auth contract) require a new `/v2/` prefix that ships alongside `/v1/` for ≥ 2 minors before `/v1/` enters its `Sunset` window. The kiln never deletes `/v1/` without ≥ 6 months of `/v2/` coexistence.

### `extra=forbid` round-trip hazard

Every response model carries `ConfigDict(extra="forbid")`. A consumer that:

1. Receives a response,
2. Deserializes through their own typed model,
3. **Re-serializes** it (e.g., to forward to a downstream worker or log),

…will trip on `extra="forbid"` the moment the kiln adds a new field. Even though "adding an optional field" is additive at the **wire** level, it's **not** additive for a consumer that uses round-trip pattern.

The decision: **document this as a known constraint, not a bug.** Consumers that round-trip MUST either:

- Use a permissive deserializer (e.g., Pydantic with `extra="allow"`) on the receiving end, OR
- Forward the raw JSON envelope without re-validating against a typed model, OR
- Pin to a specific minor and update their typed model whenever the kiln ships a new minor.

The agent guide will surface this pattern in §"Consumer round-trip" (to be added in a follow-up).

### Semver mapping to release cadence

The kiln uses a `MAJOR.MINOR.PATCH` version on the package + a `MAJOR` on the path prefix. The mapping:

- `PATCH` bumps: bugfixes, doc edits, tightening that the consumer can't observe (e.g., an internal refactor that doesn't change response shape). Never breaking.
- `MINOR` bumps: additive changes per the table above. Document in the PR; agents that switch on stable handles keep working without code changes.
- `MAJOR` bumps: any breaking change. Coincides with a new path prefix (`/v2/`) and a `Sunset` deadline on the prior prefix.

### What this doesn't cover

- **Generator-side content drift.** A new generator model (different vendor, different finetune) can produce subtly different `answer` text for the same `evidence` set. That's not a wire-format break — it's a behavior change. Documented as the `generator_model` field on `AnswerResponse` so agents can detect the switch and re-validate downstream.
- **Retrieval relevance drift.** Same query can return different chunks across releases when the ranker tunes. Not a contract break; the wire format is identical. The eval harness (`make eval` — Phase 9+) is the safety net here, not this policy.

## Consequences

### Positive

- One canonical reference for "is this change breaking?" Future PRs cite this ADR in their description instead of relitigating per change.
- Codegen consumers can plan their pinning strategy: stay on a minor if you round-trip, follow minor bumps if you switch on stable handles, hard-pin on a major.
- The drift tests already enforce most of the additive-rule mechanics (OpenAPI ↔ code enum sync). This ADR documents the policy the tests defend.

### Negative

- The pre-announce window for breaking changes (≥ 1 minor with `Deprecation` headers) slows down the cadence of cleanup work — old fields stick around longer.
- `Deprecation`/`Sunset` header emission is not yet implemented. Tracked as a follow-up under epic [#270](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/270): "Emit Deprecation + Sunset headers on responses carrying deprecated fields." Until that ships, the OpenAPI `deprecated: true` flag is the only signal.

### Neutral

- The `untrusted_content_notice_id` from PR [#305](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/305) is the canonical example of "stable handle for prose that may evolve" — codify the pattern here rather than scattering it across PRs.

## References

- PR [#266](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/266) — ErrorResponse envelope ratifying the closed-set `error_code` enum.
- PR [#299](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/299) — OpenAPI bearer security + ErrorResponse wired per-status.
- PR [#305](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/305) — `untrusted_content_notice_id` as the canonical stable-handle pattern.
- [`docs/error-codes.md`](../error-codes.md) — the per-code reference + "Adding a new code" 6-step contract.
- [`docs/agent-integration-guide.md`](../agent-integration-guide.md) §6 — the consumer-facing retry + envelope shape doc.
- [RFC 8594](https://datatracker.ietf.org/doc/rfc8594/) — `Deprecation` / `Sunset` headers.
