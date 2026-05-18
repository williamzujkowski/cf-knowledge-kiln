---
title: Eval corpus — the kiln's hand-labeled self-test
status: active
owner: platform
doc_type: reference
sensitivity: internal
last_reviewed: 2026-05-18
tags: [eval, qa]
---

## `docs/_eval/` — review-precision gold corpus

A small kiln-self-referential corpus that exercises every branch of
[`requires_human_review`](../../src/cf_knowledge_kiln/retrieval/ranking.py).
Eighteen docs, twelve hand-labeled queries; the eval suite ingests this
tree under `MockEmbeddingProvider` and scores precision against the
labels at `tests/eval/golden/review_precision.yaml`.

Production ingest is told to skip this directory via the
`exclude: - "docs/_eval/**"` rule in `config/sources.example.yaml` —
the trap doc and the deliberately-contradictory pair would poison a
real index.

## Bucket map

| Bucket | Files | Trigger |
|---|---|---|
| Clean / active | `runbook-restart-worker`, `adr-0012-asyncpg-pool-size`, `sop-requeue-stuck-ingest-job`, `standard-citation-format`, `adr-0009-rrf-k-60`, `guide-search-vs-agent-endpoints` | no warning fires |
| Conflicting pair | `auth-policy-legacy`, `auth-policy-current` | `conflicting_sources` |
| Deprecated only-match | `runbook-old-cf-push` | every-chunk-deprecated short-circuit |
| Archived filler | `archived-eleventy-bootstrap` | not a target — corpus breadth |
| Sensitive | `procedure-customer-data-access` | `sensitive_content` |
| Prompt-injection trap | `runbook-injection-trap` | `prompt_injection_pattern` |
| Weak-evidence target | `reference-model-registry` | only weak match for a distant query |
| Breadth filler | `guide-add-a-source`, `runbook-pgvector-extension-missing`, `standard-prefer-active-over-draft`, `sop-rotate-pg-credentials`, `reference-deprecation-rules` | retrieval has real choices to rank past |

## Labels live at

[`tests/eval/golden/review_precision.yaml`](../../tests/eval/golden/review_precision.yaml).
