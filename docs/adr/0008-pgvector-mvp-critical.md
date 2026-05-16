---
id: ADR-0008
title: Embeddings are MVP-critical; pgvector is back in Phase 2 scope
status: accepted
date: 2026-05-16
deciders: william
supersedes: ADR-0007
superseded_by: null
---

## Context

[ADR-0007](./0007-fts-first-embeddings-deferred.md) deferred embeddings to
a Phase 5.5 decision based on the Phase 9 eval harness. The reasoning
was: of the plan's nine ranking signals, only one (semantic similarity)
requires embeddings; pgvector deployment on the homelab BOSH CF was
multi-week work without an off-the-shelf release; ship FTS first and
prove embeddings are needed before paying the infrastructure cost.

Two things shifted that calculation:

1. **The pgvector deployment cost is no longer a multi-week effort.**
   [`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release)
   exists now (shipped 2026-05-16). It produces a working release tarball.
   The remaining work is operator deployment to the BOSH director,
   tracked in [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3).
   That changes the infrastructure cost from "multi-week BOSH packaging"
   to "operator runbook against a director we already have".
2. **The product intent is embeddings-first, not FTS-first.** Direct
   clarification from the owner: kiln is meant to ship as a CF app that
   binds to a pgvector service from MVP, not as a metadata-and-FTS app
   that adds vectors later. The eval-gated deferral pattern was the
   wrong framing for a product whose value proposition is semantic
   retrieval.

## Decision

**Reverse ADR-0007.** Embeddings are MVP-critical. Reaffirm
[ADR-0002](./0002-postgres-pgvector.md) as the active retrieval-store
decision: Postgres + pgvector, hybrid (vector + FTS) retrieval from
Phase 5.

Concretely:

- **Phase 2 schema returns to 9 tables**: `data_sources`, `documents`,
  `document_chunks`, `chunk_embeddings`, `rag_queries`, `rag_feedback`,
  `ingestion_runs`, `model_registry`, `context_packs`. `CREATE EXTENSION
  vector` is part of the initial migration.
- **Phase 4 (Embeddings)** is un-deferred. Epic [#3](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/3)
  and child issues #16, #17, #18 lose their `phase-5-5-conditional`
  label; they're active MVP work.
- **Phase 5 (Retrieval)** is hybrid from day one: vector + FTS with
  rank fusion, not FTS-then-maybe-vector.
- **Phase 5.5 decision issue** ([#36](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/36))
  is closed as "decided early: yes, embeddings — see ADR-0008".
- **Deploy gate** (epic #1 acceptance) blocks on
  [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3)
  — kiln's `cf push` requires a pgvector-enabled Postgres service
  registered with the foundation. Local development still works
  against any pgvector-capable Postgres (the `pgvector/pgvector:pg16`
  Docker image is the easiest path).

The OpenAPI contract is unchanged. Same scores, same evidence shapes;
the retrieval method behind them is now hybrid.

## ADR-0002 status

ADR-0002 was marked "superseded by ADR-0007". That note is reverted —
ADR-0002 is **accepted** again, and ADR-0007 is now superseded by
this ADR. ADR-0007's reasoning chain stays on file as a record of
the temporarily-considered alternative.

## Consequences

- **Phase 2 is bigger again.** Two extra tables, one extension install,
  plus pgvector as a runtime dep. Still tractable: the migration is
  one Alembic file.
- **Phase 5 is bigger from day one.** Hybrid rank fusion is the
  baseline, not a Phase 5.5 add-on. We don't pay the cost of designing
  a "FTS-only and then a fork to add vectors" path.
- **Local dev needs a pgvector image.** `docker-compose.yml` (added in
  Phase 2) will pull `pgvector/pgvector:pg16` for integration tests.
- **CF deployment depends on the BOSH release being operator-deployed.**
  Until [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3)
  closes, `cf push` on the homelab foundation is blocked. Local dev
  and CI are not blocked.
- **Phase 9 eval harness loses its gating role.** It's back to "prove
  we hit recall targets," not "decide whether embeddings are needed."
  Still load-bearing for Phase 9, just not for Phase 5.5 since there
  is no Phase 5.5 anymore.

## Why we changed our mind

Three honest reasons:

1. **Product intent.** The owner clarified that kiln is a pgvector-
   backed RAG CF app, full stop. FTS-first was an over-correction on
   infrastructure cost, not a product decision.
2. **Infrastructure cost dropped.** The BOSH release shipped (2026-05-16);
   deployment is now a tracked runbook, not multi-week packaging.
3. **The "eval-gated deferral" pattern was theoretical risk-reduction.**
   It would have produced a usable MVP, but a different one than the
   one the plan intended. Better to ship the intended product against
   a now-tractable deployment story than to ship a hedged version.

## Migration mechanics

The reversal is mostly removing ADR-0007-shaped scaffolding:

1. Restore `pgvector` to the `db` extra in `pyproject.toml`; drop the
   separate `embeddings` extra.
2. Mark embedding model `enabled: true` in `config/models.example.yaml`.
3. Restore "Active" status on the embedding row in `docs/model-providers.md`.
4. Re-target `manifest.yml` + `docs/deployment-cloud-foundry.md` at a
   pgvector service plan.
5. Re-include `chunk_embeddings` + `model_registry` in the Phase 2 epic
   scope; un-defer Phase 4 epic; restore hybrid framing in Phase 5 epic.
6. Drop the `phase-5-5-conditional` label from #16/#17/#18 (re-activate
   the embedding child issues).
7. Close #36 as "decided early".
8. Mark ADR-0002 status back to `accepted`; ADR-0007 superseded by 0008.
9. Update architecture.md retrieval flow to hybrid.
10. Update README + AGENTS.md + discovery-report.md to reflect the
    reversal.
11. CHANGELOG records the about-face honestly.
