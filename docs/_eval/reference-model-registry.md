---
title: "Reference: approved embedding + generator providers"
status: active
owner: platform
doc_type: reference
sensitivity: internal
last_reviewed: 2026-05-02
tags: [reference, models, providers, registry]
---

## Reference: approved providers

The kiln refuses to load a model that isn't in this registry. The
list is enforced at startup against `config/models.yaml`; the
allowlist also gates the OpenAI-compatible factory in
`ingestion/embedding.py`.

## Embedding providers

| Provider | Models | Dimensions |
|---|---|---|
| OpenAI | `text-embedding-3-small`, `text-embedding-3-large` | 1536, 3072 |
| AWS Bedrock | `amazon.titan-embed-text-v2` | 1024 |
| Nomic AI | `nomic-ai/nomic-embed-text-v1.5` (Apache 2.0, US-origin) | 768 |
| Local (sentence-transformers) | `nomic-ai/nomic-embed-text-v1.5` (default); `Snowflake/snowflake-arctic-embed-m` (alt) | 768 |
| Local | `MockEmbeddingProvider` (eval-only) | configurable |

The "Local (sentence-transformers)" row is the generic wrapper backing
the `local-sentence-transformers` provider in `config/models.yaml`.
Any sentence-transformers-compatible HuggingFace model that's also on
the [provenance allowlist](../model-providers.md) can be plugged in by
changing the `name:` field; no code change is required.

## Generator providers

| Provider | Models | Use case |
|---|---|---|
| OpenAI | `gpt-4o-mini`, `gpt-4o`, `o3-mini` | agent answer composition |
| AWS Bedrock | `anthropic.claude-3-5-sonnet`, `anthropic.claude-3-5-haiku` | agent answer composition |
| Local | `MockGenerator` (eval-only) | journey tests |

## Why so short

Two reasons:

- The registry must be reviewable in a single screen. Every entry
  carries a real procurement + security review; we don't add
  speculative providers.
- AGENTS.md hard constraint: **no US-adversary-origin models**.
  Qwen, DeepSeek, BAAI/BGE are out. Anthropic, OpenAI, AWS Bedrock,
  vetted local-weights variants are in.

## Adding a provider

File an ADR under `docs/adr/`. Include the procurement record, the
data-residency notes, and a baseline retrieval-quality run against
the bootstrap golden set. Approved ADRs land their provider entry
here in the same PR.
