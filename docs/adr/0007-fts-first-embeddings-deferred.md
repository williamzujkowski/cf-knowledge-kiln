---
id: ADR-0007
title: Hybrid retrieval starts with BM25 + metadata; embeddings deferred to a Phase 5.5 decision
status: accepted
date: 2026-05-16
deciders: william
supersedes: ADR-0002
superseded_by: null
---

## Context

[ADR-0002](./0002-postgres-pgvector.md) committed us to Postgres + pgvector as
the initial vector store. That decision was anchored to the plan's
"Postgres + pgvector" call-out and to the assumption that semantic
search would be load-bearing for the MVP. Two subsequent discoveries
changed the calculation:

1. **The plan's own ranking criteria are mostly not vector-based.** Of
   nine ranking signals (semantic similarity, keyword match, document
   status, authority level, last reviewed date, source owner, exact
   heading/title match, control/tag match, feedback signals), only
   *semantic similarity* requires embeddings. Seven are metadata, one
   is FTS. The retrieval method is doing less work than the metadata
   model is.
2. **No off-the-shelf pgvector-enabled BOSH Postgres release exists.**
   `cloudfoundry/postgres-release` v54.0.1 (2026-05-14) ships without
   pgvector. The `cloud-gov/opensearch-boshrelease` is logging-focused
   and ships no k-NN plugin. Adding either capability is multi-week
   BOSH packaging work. The deployment cost of embeddings is higher
   than the deployment cost of FTS by an order of magnitude on the
   homelab's BOSH-on-Incus CF.

We also have a Phase 9 deliverable — a retrieval evaluation harness —
that *should* be the evidence base for whether embeddings improve
retrieval on our actual corpus. Shipping pgvector before that evidence
exists is speculative.

## Decision

The MVP retrieval substrate is **Postgres FTS (`tsvector`) + rich
metadata filtering + authority/freshness ranking**. No embeddings, no
pgvector, no vector index of any kind in the initial schema.

Embeddings are deferred to a **Phase 5.5 decision** gated on the Phase 9
eval harness. If the eval shows retrieval quality below the target
threshold on our corpus, Phase 5.5 adds embeddings via pgvector. If
not, we never need them and the entire embedding subsystem stays
unbuilt.

Concretely:

- **Phase 2 schema**: `data_sources`, `documents`, `document_chunks`,
  `rag_queries`, `rag_feedback`, `ingestion_runs`, `context_packs`.
  Seven tables, not nine. `chunk_embeddings` and `model_registry` are
  dropped from the initial migration; they land in a single follow-up
  migration if Phase 5.5 picks embeddings.
- **Phase 4** (Embeddings) is marked deferred / conditional on Phase 5.5.
- **Phase 5** (Retrieval) ships BM25 + metadata + ranking. Hybrid is
  still the goal — "hybrid" just means lexical + metadata for now,
  not lexical + vector.
- **Phase 5.5** (new, conditional): "Decide on embeddings based on
  eval-harness results." See the corresponding GitHub issue.

The retrieval-method change is **invisible at the API boundary**. The
OpenAPI contract (`SearchResponse`, `ContextPackResponse`) has no
embedding-specific fields; scores are still scores, evidence is still
evidence. No public-API breakage.

## Parallel work

A separate repository,
[`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release),
exists in parallel as a reusable BOSH release for any CF team that
needs pgvector. As of [PR #1](https://github.com/williamzujkowski/bosh-pgvector-release/pull/1)
(2026-05-16) it produces a buildable 93 MB release tarball with
pgvector compiled against the bundled postgres-15, postgres-16, and
postgres-17 packages, ready for `bosh upload-release`. It is **not a
dependency of this app's MVP**. It will become the deployment path
for Phase 5.5 if/when embeddings get the green light from the eval
harness. Until then, the two repos are fully decoupled — kiln ships
on stock CF Postgres, and the BOSH release stands on its own merit
for the community.

## Consequences

- **Phase 2 ships in days, not weeks.** Stock `cloudfoundry/postgres-release`
  satisfies the Postgres binding requirement. No BOSH packaging work
  on the critical path. No UPSI workaround. The homelab BOSH CF is
  unblocked immediately.
- **The eval harness becomes the decision-making artifact.** Phase 9
  is no longer "evaluate retrieval quality"; it's "evaluate retrieval
  quality AND tell us whether to add embeddings." That promotes
  Phase 9 from polish to architecture-deciding.
- **We lose pure semantic matching** until/unless Phase 5.5 adds it.
  Concretely: queries that use different vocabulary than the docs
  ("SSO" vs "authentication") will rank lower. For technical
  documentation written by the same people who write the queries
  (the homelab case), the impact is empirically small. For broader
  corpora, this may not hold; the eval harness will surface it.
- **Reranking can come before embeddings if BM25 isn't enough.** If
  the eval shows BM25 weakness, we have two upgrade paths: add a
  cross-encoder reranker on the top-K (no new index, no BOSH work),
  or go full embeddings via Phase 5.5. The lighter option is tried
  first.
- **ADR-0002 is superseded** but kept on file for context — the
  reasoning there is still valid as the *eventual* architecture if
  Phase 5.5 picks embeddings; we are just deferring the commitment.

## Alternatives considered (briefly, in addition to ADR-0002's list)

- **OpenSearch via cloud-gov/opensearch-boshrelease + k-NN** — release
  exists but ships no k-NN plugin; would need ~1–3 weeks of additional
  BOSH packaging *plus* an ADR-0002 reversal. Net: more work than
  pgvector for the same outcome. Rejected.
- **Redis Stack + RediSearch via cf-redis-broker fork** — RAM-bound,
  no transactional consistency with the document store, broker fork
  required. Rejected.
- **Long-context stuffing (no retrieval at all)** — viable for the
  current `homelab-iac` corpus (~500K–1M tokens) but does not scale to
  multi-source ingestion. Doesn't compose with the agent context-pack
  API (which exists *because* bounded evidence is valuable). Rejected
  as the MVP path; revisit if we ever ship a "single-source, single-
  user" mode.
- **GraphRAG / RAPTOR** — heavy ingestion cost, brittle to LLM
  extraction errors, overkill for documentation. Skipped.

## Migration path if/when Phase 5.5 picks embeddings

The chunk store and metadata model are forward-compatible. Adding
embeddings later means:

1. New Alembic migration: create `chunk_embeddings` and `model_registry`
   tables, install the `vector` extension on the bound database.
2. Bind the app to a pgvector-enabled service (likely via
   [`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release)
   on the homelab CF).
3. Backfill embeddings for existing chunks via a one-shot worker job.
4. Update the retrieval orchestrator to merge vector + FTS rankings.

No app-side rearchitecture. The retrieval orchestrator is the only
component that knows about retrieval methods; everything else operates
on chunks + metadata.
