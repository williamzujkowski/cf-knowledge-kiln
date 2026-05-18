---
title: "ADR-0009: hybrid retrieval fuses with RRF k=60"
status: approved
owner: platform
doc_type: adr
sensitivity: internal
last_reviewed: 2026-04-15
tags: [adr, retrieval, ranking, rrf]
---

## ADR-0009: hybrid retrieval fuses with RRF k=60

## Status

Approved 2026-02-04. Implemented in
`src/cf_knowledge_kiln/db/repositories/_hybrid.py`.

## Context

The kiln's retrieval engine runs two arms in parallel:

1. **Vector** — pgvector similarity against the chunk embeddings.
2. **FTS** — `ts_rank_cd` against the chunk's tsvector.

We need a fusion that's stable when one arm scores well and the other
returns junk (the typical case for short queries like "rrf" — vector
arm is noisy, FTS arm carries the signal).

## Decision

Reciprocal Rank Fusion with `k = 60`. The score per chunk is

```text
score = 1 / (k + rank_vec) + 1 / (k + rank_fts)
```

`k=60` is the published TREC default and matches every RRF eval result
we ran. Smaller `k` over-weights the top-1 of each arm and causes
jitter near the boundary; larger `k` flattens the curve so much that
the two arms barely interact.

## Consequences

- The score is not directly interpretable as similarity. The
  `--score` field in result cards is the fused number; the
  weak-evidence threshold (`WEAK_EVIDENCE_SCORE_THRESHOLD` in
  `retrieval/ranking.py`) is calibrated against this scale.
- `k` is not a tunable. Changing it would invalidate every threshold
  and bootstrap floor downstream. A future ADR may revisit if we
  switch to a learned fusion.
- The fused score is RECOMPUTED, not cached. If we ever add result
  caching, the cache key must include the model name + chunker
  version so a model swap can't return stale fused scores.
