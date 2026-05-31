---
id: ADR-0013
title: HyDE query expansion behind a default-off flag
status: accepted
date: 2026-05-31
deciders: william
supersedes: null
superseded_by: null
---

## Context

The Phase 5 hybrid retriever (ADR-0009) combines pgvector cosine similarity with Postgres FTS via Reciprocal Rank Fusion. The vector arm performs poorly on three query shapes the calibration-222 eval (`tests/eval/calibration-222-results-2026-05-24.md`) made visible:

1. **Short queries** — `"offsite backup failed"` carries too few tokens for the vector arm to disambiguate among multiple offsite-backup-adjacent docs.
2. **Jargon-dense queries** — operator-speak (`"credhub ca rotation"`, `"BBR director restore"`) embeds far from the natural-prose chunks the real docs contain.
3. **Imperative-style queries** — `"how do I rotate..."` matches against indexed prose like `"To rotate, run..."` — same intent, different surface form.

8 of the 13 calibration-222 positives missed expected-source-in-top-5. PRs #332 and #333 shipped the HyDE substrate (classifier + cache + engine) and wired it into `HybridRetriever` behind `KILN_HYDE_ENABLED`. This ADR records why HyDE was chosen, why it's default-off, and what the calibration eval has (and has NOT) told us.

## Decision

Ship HyDE as the query-rewriting strategy. Default off. Three reasons over the alternatives:

1. **No new dependency vocabulary.** HyDE uses the existing `GeneratorProvider` interface that already powers `/v1/answer`. No new model, no new prompt corpus.
2. **Gracefully bypassable.** The classifier gate (`should_hyde`) declines on long chatty queries that already have signal; the cache absorbs repeat traffic; failure modes (no generator, generator error, empty output) all degrade silently to bare retrieval. The retriever NEVER turns a successful search into a 503 because of HyDE.
3. **Substrate composes with later strategies.** A future multi-query / dictionary-expansion / symmetric-prefix strategy can wire into the same `_embedding_text_for_vector_arm` helper without re-touching the retriever.

### Alternatives considered

* **Multi-query (generate N paraphrases, embed each, fuse the candidate sets)** — strictly more expensive (N generator calls + N vector queries + a fusion pass). The marginal lift over HyDE on operator-style queries is unproven against this corpus.
* **Dictionary expansion (server-side synonym table)** — operationally cheap but adds a corpus-curation burden (who edits the synonyms? when?). HyDE generalizes via the LLM's general-purpose embedding instead of a hand-curated vocabulary.
* **Symmetric-prefix retrieval (embed the query with the doc-side prefix instead of the query-side prefix)** — already tested in #228. The prefix-swap moved scores but not hit rate. Different problem.

### Default-off rationale

Three reasons the master flag defaults to `false`:

* **Generator dependency.** A clean MVP install has no LLM wired up (`KILN_GENERATOR_BASE_URL` unset → `MockGeneratorProvider` only). Default-on with no generator would log a startup warning every restart and add no behavior. Default-off matches the no-generator common case.
* **Cost surface.** Each `should_hyde=True` query pays one extra LLM round-trip on a cache miss. Operators with strict budget or strict latency requirements opt in.
* **Eval gap (see Consequences).** The calibration-222 corpus contains a corpus-drift confound that limits how strongly we can endorse HyDE today. Default-on without a clean A/B win would over-claim.

### Configuration

Seven `KILN_HYDE_*` env vars, all documented in `docs/configuration.md#hyde-query-expansion`:

```text
KILN_HYDE_ENABLED                   master switch (default false)
KILN_HYDE_QUERY_TOKEN_THRESHOLD     short-query gate (default 8 tokens)
KILN_HYDE_JARGON_DENSITY_THRESHOLD  jargon gate (default 0.4)
KILN_HYDE_CACHE_MAX_ENTRIES         in-process cache cap (default 1000)
KILN_HYDE_CACHE_TTL_SECONDS         cache freshness (default 86400 / 24h)
KILN_HYDE_GENERATOR_MAX_TOKENS      pseudo-doc cap (default 200)
KILN_HYDE_GENERATOR_TIMEOUT_SECONDS reserved for #334 (declared but not wired today)
```

### Observability

One new OTel span: `retrieval.hyde` with the `retrieval.hyde.gated_on` attribute (boolean — true when the engine emitted a pseudo-doc, false when classifier declined / no generator / cache hit was empty). `cache_hit` and `generation_ms` attributes filed as follow-up under issue #404 — they require refactoring `HydeEngine.expand` to return a result object alongside the pseudo-doc.

## Consequences

### What we expect to win

* **AGENTS.md long-section misses (q08 / q09 / q11 in the calibration-222 set).** The 1,429-token `AI Agent Instructions > Credential Access > Rotation coverage` section embeds with the average of all its sub-topics; a HyDE pseudo-doc for `"where is the Cloudflare DNS token stored"` embeds closer to that specific topic and should improve ranking.
* **Operator-style queries that today match no specific section** because the query is jargon-dense (`"BBR director restore command"`). HyDE generates a paragraph of declarative prose using the right domain language, which closes the embedding-space gap to indexed prose.

### What we cannot yet claim

The diagnostic from `tests/eval/reports/chunking-misses-vs-section-size-2026-05-30.md` (#335) showed that **5 of 8 calibration-222 misses reference docs that don't exist in the indexed corpus** (filed as #399). Those misses are corpus-drift, not retrieval-quality. HyDE cannot fix a doc that isn't in the corpus.

So the available calibration A/B has a ceiling: at most 3/8 misses can move (the AGENTS.md long-section bucket). A 3/8 → 0/8 lift would be a 23 pp jump in expected-in-top-5 (rising from 5/13 = 38 % to 8/13 = 62 %), which is real but resting on a small denominator. The full eval-ratchet decision (acceptance criterion `Win` in issue #334) is **deferred until #399 lands** and the golden set is reconciled with what's actually in the corpus.

### What landed today (PR #334)

* `tests/eval/run_calibration_eval.py` accepts `--with-hyde` so the report markdown labels the arm. Operators run `KILN_HYDE_ENABLED=true make run` plus this flag for the HyDE arm; default flag for the baseline arm. Side-by-side comparison is a manual diff today.
* `docs/configuration.md` documents the seven env vars.
* `docs/observability.md` documents the `retrieval.hyde.gated_on` span attribute.
* This ADR.

The per-bucket floor changes in `tests/eval/test_golden.py` are **NOT** in this PR — the kiln's own docs/ golden set has too few cases per bucket to drive a meaningful ratchet, and the homelab-iac golden set is corpus-drift-blocked until #399. Filing as a follow-up against #233 once both prerequisites land.

### Cost story

Per-query when gated on:

* +1 LLM round-trip on cache miss (typically 50-200 ms wall-clock against a self-hosted Llama-class model).
* +1 small in-memory cache write.
* No additional DB round-trip — the embedding API still gets one `embed_query` call; HyDE only changes what text gets embedded.

Per-query when gated off (the dominant case for long chatty queries):

* +1 cheap regex + token-count classification (microseconds).
* Zero generator / cache / DB cost.

## Status

Accepted. Substrate lives in `src/cf_knowledge_kiln/retrieval/hyde/`. Wiring lives in `src/cf_knowledge_kiln/retrieval/engine.py` (`_run_query` consults `_embedding_text_for_vector_arm`). Lifespan construction lives in `src/cf_knowledge_kiln/api/app.py` (HyDE only constructed when `hyde_enabled=true` AND a generator is configured).

Ratchet decision (default-off → default-on) deferred to #399 + the next calibration-228-style A/B that follows it.
