---
id: ADR-0005
title: Model provider abstraction with US-origin MVP allowlist
status: accepted
date: 2026-05-16
deciders: william
---

## Context

We need to support multiple embedding and generator providers over
time. Locking the application to a single provider would force code
changes every time we swap models. The plan also has a constraint:
the MVP avoids China-origin model families (Qwen, DeepSeek, BAAI/BGE).

## Decision

- Models are referenced through a small provider abstraction:
  `EmbeddingProvider` and `GeneratorProvider` interfaces. Providers
  are selected by `config/models.yaml`.
- The MVP ships two provider adapters: `local` (a process-local model
  loaded at boot) and `openai-compatible` (any HTTP endpoint that
  speaks the OpenAI chat/embeddings API). Secrets for the latter live
  in env vars / CF bindings.
- The MVP active models, subject to provenance review:
  - Embeddings: `nomic-embed-text-v1.5` (Nomic AI, US, Apache 2.0).
  - Generator: `microsoft/Phi-4-mini-instruct` (Microsoft, US, MIT-
    style license). Disabled by default; enabled when Phase 5 lands
    the `/v1/answer` endpoint.
- Embedding dimensionality is stored per-row in `chunk_embeddings`.
  Re-embedding requires a model-change job; the schema does not
  assume one dimension.
- Adding a new model to the active set requires a documented
  provenance + license check in `docs/model-providers.md`.

## Consequences

- Model swap = config change, not code change.
- We never silently change embedding dimensions for existing rows.
- Provenance is captured in source, not in someone's memory.
- We are explicit about the China-origin exclusion: the *constraint*
  lives in the plan, the *implementation* of that constraint is the
  `docs/model-providers.md` allowlist that PRs touching this file get
  scrutinized against.

## Alternatives considered

- **Bake a single provider in** — simpler now, expensive later.
  Rejected.
- **No allowlist; let any model in** — violates the plan's hard
  constraint. Rejected.
- **Use one of the excluded model families** — fast, but blocked by
  the plan. Rejected.
