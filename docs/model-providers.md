# Model providers

This document is the **active allowlist**. PRs that touch `config/models.yaml`
get reviewed against this file. See [ADR-0005](./adr/0005-model-provider-abstraction.md)
for the architectural rationale.

## Allowed for MVP

| Role        | Name                              | Provider/Origin    | License                      | Status (MVP) |
| ----------- | --------------------------------- | ------------------ | ---------------------------- | ------------ |
| Embedding   | `nomic-embed-text-v1.5`           | Nomic AI (US)      | Apache 2.0                   | Active (MVP per [ADR-0002](./adr/0002-postgres-pgvector.md) / [ADR-0008](./adr/0008-pgvector-mvp-critical.md)) |
| Generator   | `microsoft/Phi-4-mini-instruct`   | Microsoft (US)     | Microsoft Research License   | Disabled until Phase 5 lands `/v1/answer` |

## Possible additions, subject to provenance review

- Small Llama-family generators (Meta, US).
- Other US-origin sentence-transformer embeddings with a permissive license.

## Excluded for MVP

The MVP avoids China-origin model families. This is a plan-level
constraint, captured here so PRs do not have to re-derive it:

- `Qwen` family
- `DeepSeek` family
- `BAAI/BGE` family

Removing a model from this list requires an ADR.

## Adding a new model

1. Confirm provenance (publisher org country / jurisdiction).
2. Confirm license is compatible (MIT, Apache 2.0, BSD, MSR-license,
   Llama-community-license — check terms).
3. Add a row to the **Allowed for MVP** table above.
4. Update `config/models.example.yaml`.
5. If swapping the active embedding model, schema-wise we are safe
   (`chunk_embeddings.dimensions` is per-row), but document the
   reindex requirement.

## Why the China-origin exclusion?

The plan calls it as a hard constraint for the MVP. It is not a
permanent policy; it is a constraint scoped to this MVP. The
implementation enforces it through review of this allowlist and of
`config/models.yaml`, not through a runtime block — model files are
fetched out-of-band, so a runtime block would be a false sense of
control.
