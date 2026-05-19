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

## Embedding modes

The review-precision suite runs in two modes:

| Mode | Trigger | Provider | Test coverage |
|---|---|---|---|
| Mock (default) | unset | `MockEmbeddingProvider` (degenerate vector arm) | binary `requires_human_review` precision only |
| Real | `KILN_EVAL_REAL_EMBEDDINGS=1` | `LocalSentenceTransformersProvider` with Nomic Embed v1.5 (768d) | binary precision + per-bucket confidence calibration |

The default keeps the gate stable on CI without the heavy
`sentence-transformers` + `torch` install; the opt-in path is for
the confidence-calibration tier landing under #108 item 2.

### Real-embedding mode environment

| Env var | Default | Purpose |
|---|---|---|
| `KILN_EVAL_REAL_EMBEDDINGS` | unset (mock) | set to `1` to swap in Nomic Embed v1.5 |
| `KILN_EMBEDDING_DEVICE` | `cpu` | passed through to the local provider (`cpu`/`cuda`/`mps`) |

The real-embedding path requires the `embeddings` extra:

```bash
pip install 'cf-knowledge-kiln[embeddings]'
KILN_EVAL_REAL_EMBEDDINGS=1 make eval
```

When the extra is missing under `KILN_EVAL_REAL_EMBEDDINGS=1`, the
calibration test skips with a pointer to the install command rather
than silently falling back to mock — the mock-vs-real signal is
exactly what the calibration tier exists to measure.

## Multi-relevance grades (strawman)

Each case in `review_precision.yaml` may carry an optional
`relevance:` block mapping the four-part citation
(`<repo>/<path>#<H1>/<H2>/...`) to a 0..3 grade per the rubric:

| Grade | Meaning |
|---|---|
| 3 | Perfect answer to the query |
| 2 | Useful, partial |
| 1 | Tangentially relevant |
| 0 | Irrelevant |

The current grades are **strawman** — authored by reading the corpus
and guessing what each query's top-K would contain. Every block is
tagged with the `# strawman — needs human spot-check` marker. The
per-bucket calibration scorer uses these grades only when
`KILN_EVAL_REAL_EMBEDDINGS=1`; the binary precision test ignores
them, so cases without grades load and run normally.
