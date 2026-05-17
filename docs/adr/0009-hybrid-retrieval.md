---
id: ADR-0009
title: Hybrid retrieval — RRF over pgvector + Postgres FTS
status: accepted
date: 2026-05-16
deciders: william
supersedes: null
superseded_by: null
---

## Context

Phase 5 (epic [#4](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/4))
implements `/v1/search` and `/v1/agent/context-pack` over the
ingested+embedded corpus. [ADR-0008](./0008-pgvector-mvp-critical.md)
fixed the architecture as **hybrid from day one**: pgvector vector
similarity and Postgres FTS together, not FTS-first-with-vectors-later.

The plan enumerates nine ranking signals — semantic similarity, keyword
match, document status, authority, last_reviewed, owner, exact heading/
title match, control/tag match. The signals have heterogeneous scales
(cosine distance in `[0, 2]`, ts_rank_cd in unbounded positives, status
weight in `[0, 1]`). Two questions follow:

1. How do we merge the vector and lexical arms into one ranking?
2. What query-time defaults do we set on the existing HNSW + GIN indexes?

The schema (migration 0001) already gives us:

- `chunk_embeddings.embedding` (unconstrained `vector`) with a **partial
  HNSW index** keyed to `dimensions = 768` (cosine_ops).
- `document_chunks.content` with a **GIN index** on
  `to_tsvector('english', content)`.
- `documents` with btree indexes on `status`, `repo`, `doc_type`,
  `last_reviewed` — i.e., the predicates we'd want to push into the
  WHERE clause.

## Decision

Phase 5 retrieval uses the following seven choices, each documented
here so future readers can see why we did not pick the obvious
alternatives.

### 1. Vector query-time parameter — `hnsw.ef_search = 200`

`SET LOCAL hnsw.ef_search = 200` at the start of every retrieval
transaction. Targets ~95% recall@10 at p95 < 50 ms on a 100k-chunk
corpus. Exposed as `KILN_HNSW_EF_SEARCH` so operators can tune for
their own corpus size and latency budget without a code change.

The default 40 (pgvector built-in) is too aggressive for our recall
target on small corpora. 400+ pays a noticeable latency cost without
materially improving recall@10. 200 is the common operating point in
the literature; we can revisit with measurements from the Phase 9 eval
harness.

### 2. FTS rank function — `ts_rank_cd`

`ts_rank_cd` (cover-density) rewards passages where query terms appear
close together. We use it for the FTS arm of the hybrid score. Exact
heading/title boosts are layered separately at the fusion + re-ranking
stage; the FTS function only needs to score the chunk content.

`ts_rank` (term-frequency only) was the alternative. Rejected because
proximity matches the retrieval intent better — a passage with two
query terms in adjacent sentences is more relevant than a passage with
two terms 200 lines apart, and `ts_rank_cd` is what captures that.

### 3. Hybrid merge — Reciprocal Rank Fusion (RRF), k = 60

For each retrieved chunk `d`:

```text
score(d) = 1/(k + rank_vector(d)) + 1/(k + rank_fts(d))   # k = 60
```

A chunk only appearing in one arm uses `+∞` for the missing rank
(equivalently, omits that term). The top-20 by `score(d)` go to
re-ranking.

RRF is parameter-light (one constant), scale-invariant (we don't have
to calibrate cosine distance against `ts_rank_cd` magnitude), and
robust to corpus growth. `k = 60` is the standard from the Cormack
et al. paper and the common default in real systems.

Weighted-normalization (min-max or z-score against each arm's score
distribution) is the obvious alternative. Rejected because it forces
per-arm calibration that drifts as the corpus grows and as we add
embedding models — and we get a tuning surface (vector weight vs.
FTS weight) we don't yet have signal to set well. We can revisit once
the Phase 9 eval harness produces query-judgment data, but RRF is the
right MVP starting point.

### 4. Metadata filter pushdown — into the WHERE clause, before recall

Status, sensitivity, repo, doc_type, owner, last_reviewed_after, and
control/tag predicates are pushed into the WHERE clause on **both** the
vector and FTS arms before the candidate union — not applied after
RRF.

The HNSW partial index honors `WHERE dimensions = 768`; an additional
`WHERE status = ANY($1::text[])` filter is a plain predicate the
planner handles. The btree indexes from migration 0001 carry the
selective ones. Filtering *after* fusion would dilute recall (we'd
fetch top-100 from each arm, fuse, then drop everything that didn't
match `status = active`, possibly leaving 3 results from a top-20
pool).

The cost: if the filters are very non-selective (e.g., status =
'active' returns 95% of rows), the HNSW post-filter step becomes
slightly slower than the unfiltered baseline. We accept this; it's
the case where we're paying a small cost for *correct* results
instead of a small saving on incorrect ones. Iterate if it ever shows
up in profiles.

### 5. Single SQL round-trip — via CTE

Phase 5 acceptance: "single SQL round-trip preferred." We meet this
with a CTE pattern:

```sql
WITH vec AS (
  SELECT chunk_id,
         ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector ASC) AS rnk
    FROM chunk_embeddings
   WHERE dimensions = 768
   LIMIT 100
),
fts AS (
  SELECT dc.id AS chunk_id,
         ROW_NUMBER() OVER (
           ORDER BY ts_rank_cd(to_tsvector('english', dc.content), q) DESC
         ) AS rnk
    FROM document_chunks dc,
         plainto_tsquery('english', $2) q
   WHERE to_tsvector('english', dc.content) @@ q
   LIMIT 100
),
fused AS (
  SELECT chunk_id,
         SUM(1.0 / (60 + rnk)) AS rrf
    FROM (SELECT * FROM vec UNION ALL SELECT * FROM fts) u
   GROUP BY chunk_id
)
SELECT dc.*, d.*, fused.rrf
  FROM fused
  JOIN document_chunks dc ON dc.id = fused.chunk_id
  JOIN documents d ON d.id = dc.document_id
 WHERE d.status = ANY($3::text[])
   AND (d.repo IS NULL OR d.repo = ANY($4::text[]))
   -- … other metadata predicates …
 ORDER BY fused.rrf DESC
 LIMIT 20;
```

If a future per-signal weighting is added (authority boost, freshness
decay), it layers on as a multiplier inside `fused` or after. The CTE
stays the structural shape.

### 6. Multi-model HNSW indexes — operator-explicit, one migration per dimension

When a second embedding model with a different dimension is registered,
its partial HNSW index is created **in a new Alembic migration**, not
auto-created at runtime. The naming contract is
`ix_chunk_embeddings_hnsw_<dimensions>`.

Operator runbook (filed as a Phase 9 task):

```sql
CREATE INDEX CONCURRENTLY ix_chunk_embeddings_hnsw_1024
  ON chunk_embeddings
  USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
  WHERE dimensions = 1024;
```

Runtime auto-creation was the alternative. Rejected because building
an HNSW index on a populated table locks writes for the duration —
which during an ingest spike is exactly the wrong time for a worker
to decide it needs a new index. Operator-explicit puts the cost in a
known maintenance window.

### 7. Candidate pool — top-100 per arm → RRF → top-20

`LIMIT 100` per arm. RRF over the union. Top-20 returned to the caller
(re-ranking layer applies authority/freshness/status boosts to the
top-20 only).

100 is enough headroom for RRF to recover from each arm's blind spots
(a recall@100 of ~99% per arm gives a fused recall@10 that's strictly
better than either alone). Going to 200+ per arm doesn't measurably
help recall on a 100k-chunk corpus and roughly doubles the FTS arm's
work.

## Consequences

- **Single SQL round-trip per query.** The pgvector + FTS arms execute
  in one Postgres statement; rank fusion is in SQL, not in Python.
- **Per-row dimensionality is preserved.** Chunks from different
  embedding models can coexist; each query embeds in one model's
  dimension and filters to that.
- **HNSW index params are tunable.** `KILN_HNSW_EF_SEARCH` is the lever
  for the recall/latency trade. The default (200) is the documented
  starting point.
- **Multi-model setups need operator migrations.** Documented in the
  runbook; no automatic schema mutation at runtime.
- **The Phase 9 eval harness can revisit RRF vs. learned-to-rank.**
  RRF is the MVP choice, not the permanent answer. Tracking issue
  filed under Phase 9.

## Alternatives considered

- **Two SQL round-trips with Python-side fusion** — simpler code, more
  network hops, harder to push metadata filters cleanly. Rejected.
- **Weighted normalization** — gives a tuning surface we can't yet
  calibrate. Rejected as the MVP merge; revisitable.
- **Pure pgvector or pure FTS** — covered by ADR-0008. Both have known
  blind spots; hybrid was the decision.
- **Application-level rank fusion with a separate vector store** — adds
  an operational service. Rejected; the Postgres-only story is the
  product value.
