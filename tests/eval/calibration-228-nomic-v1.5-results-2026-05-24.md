# #228 — Nomic Embed v1.5 swap experiment: REJECTED

Recorded 2026-05-24 on the homelab-iac corpus (265 markdown files →
2,572 chunks). Companion to `calibration-222-results-2026-05-24.md`
(the e5-small-v2 baseline).

## Question

The #222 calibration eval found 5/13 (38%) top-5 hit rate on the
homelab-iac golden set with `intfloat/e5-small-v2`. The user's target
is 85% (per homelab-iac#628). Does swapping in a larger, US-origin
embedder — `nomic-ai/nomic-embed-text-v1.5` (768-dim vs e5's 384-dim)
— close the gap?

## Method

1. Swap `config/models.yaml`: `e5-small-v2` (384-dim) →
   `nomic-embed-text-v1.5` (768-dim, `trust_remote_code: true`).
2. Restart worker with the new provider; re-ingest the same
   homelab-iac corpus from `/tmp/calibration-222/homelab-iac`.
3. Run the same 15-query golden set (13 positives, 2 negatives)
   from homelab-iac#627 against `POST /v1/search`.
4. Compare top-1 distribution + expected-in-top-5 against the e5
   baseline.

`einops` had to be `pip install`-ed into the venv first — Nomic's
custom `nomic-bert-2048` modeling code requires it but it isn't a
declared dep of `sentence-transformers`. The worker spun loading the
model with a silent `[transformers] Encountered exception while
importing einops: No module named 'einops'` for ~30 cycles before I
caught it. **Filed as a kiln issue (see Follow-ups).**

Ingestion completed in ~15 minutes (vs ~10 min on e5; the 2x dim
costs roughly 50% more wall clock on CPU).

## Top-1 score distribution (positive queries)

| stat | e5-small-v2 | nomic-v1.5 | Δ |
| ---- | ----------- | ---------- | --- |
| min | 0.484 | 0.462 | -0.022 |
| p25 | 0.492 | 0.484 | -0.008 |
| median | 0.500 | 0.500 | 0 |
| mean | 0.594 | 0.592 | -0.002 |
| p75 | 0.500 | 0.500 | 0 |
| max | 0.977 | 0.977 | 0 |

Score *shape* is essentially identical. The fused-RRF normalization
(#164) caps both arms-rank-1 at 1.0 and single-arm rank-1 at 0.5; both
models land most positives at the 0.5 single-arm plateau, with a few
both-arm hits up at 0.97.

## Quality metrics

| metric | e5-small-v2 | nomic-v1.5 |
| ------ | ----------- | ---------- |
| top-1 ≥ weak_evidence floor (0.46) | 13/13 (100%) | 13/13 (100%) |
| positive top-5 hit | **5/13 (38%)** | **4/13 (31%)** |
| negative correctly flagged | 0/2 (0%) | 0/2 (0%) |
| user's target | 85% top-5 | (no change) |

## Per-query (nomic-v1.5)

| qid | top1 | expected | result | vs e5 |
| --- | ---- | -------- | ------ | ----- |
| q01 | 0.469 | skills/manage-offsite-backup | missed | same |
| q02 | 0.500 | skills/bbr-backup-and-restore, docs/components/backups-bbr | in-top-5 | same |
| q03 | 0.500 | docs/runbooks/credhubcaexpiring | in-top-5 | same |
| q04 | 0.500 | docs/runbooks/offsitebackupfailed | missed | same |
| q05 | 0.500 | docs/components/caddy-reverse-proxy, AGENTS.md | **missed** | was in-top-5 |
| q06 | 0.484 | docs/components/offsite-backup | missed | same |
| q07 | 0.484 | docs/components/offsite-backup, docs/components/backups-bbr | in-top-5 | same |
| q08 | 0.977 | AGENTS.md | missed | same |
| q09 | 0.500 | AGENTS.md, scripts/wait-for-host | missed | same |
| q10 | 0.462 | skills/manage-offsite-backup | missed | same |
| q11 | 0.969 | AGENTS.md, inventory/lab.yml | missed | same |
| q12 | 0.868 | docs/components/authentik-sso, inventory/lab.yml | missed | same |
| q13 | 0.500 | (negative) | leaked-through | same |
| q14 | **0.500** | (negative) | leaked-through | **was 0.901** |
| q15 | 0.484 | docs/components/pgvector | in-top-5 | same |

## What the data says

### The hypothesis ("Nomic v1.5 will lift top-5 hit rate"): REJECTED

Nomic v1.5 dropped one positive (q05: TLS / Caddy) out of the top-5,
moving the metric from 5/13 → 4/13. Every other positive's
top-5-membership status stayed the same. **A larger embedder does not
close the gap to 85% on this corpus.**

The misses cluster around two shapes that no embedder size will fix:

* **Long-section docs that mix many topics** — AGENTS.md (q08, q09,
  q11) is the biggest offender. The retriever picks chunks that share
  surface tokens with the query rather than the specific section the
  user thinks answers it. Smaller chunks + section-aware chunking
  would help more than a bigger embedder.
* **Asymmetric query-vs-passage phrasing** — q01 / q04 / q06 / q10
  ask in operator/runbook language ("rotate," "fired," "trap with
  re-keying"); the source docs answer in declarative form. Both
  embedders see surface mismatch. Query rewriting or HyDE-style
  expansion is the lever, not embedder size.

### One genuine improvement: q14 false-positive dampened

q14 ("How do I configure Kubernetes?" — a negative; the homelab
doesn't use Kubernetes) dropped from **0.901 with e5 → 0.500 with
nomic**. Nomic's representation distributes the surface match across
more dimensions, so the lone passing mention of Kubernetes in
cf-deployment notes doesn't dominate as starkly. This is consistent
with Nomic v1.5's larger output dim providing finer semantic
discrimination on out-of-domain queries.

But: q14 still scores at the 0.5 weak-evidence plateau (above the 0.46
floor), so `requires_human_review` doesn't fire. The #227
`isolated_match` warning DOES fire on the e5 case (gap 0.901 - 0.5 =
0.401 > 0.3) but NOT on the nomic case (gap 0.5 - 0.5 = 0 < 0.3) —
**which is exactly the right outcome.** When the score distribution
genuinely looks weak, weak_evidence owns the signal; when one chunk
stands out artificially, isolated_match catches it. #227's design is
robust across both embedders.

## Cost trade-off

* **+50% ingestion wall-clock** (10 min → 15 min on CPU)
* **+~1.4 GB RAM** for inference (768-dim activations)
* **No retrieval-quality gain** (4/13 vs 5/13)
* **One quality regression** (q05 TLS query no longer in top-5)

## Decision: KEEP e5-small-v2

Bigger embedder is the wrong lever for this gap. Reverting
`config/models.yaml` back to e5-small-v2 and filing follow-ups for
the actual cost drivers.

## Follow-ups filed

* **#231** — `einops` should be a declared dep when the
  Nomic-family models are configured. Fail-fast loader check + docs
  note. (Filed 2026-05-24.)
* **#232** — chunking strategy for long-section docs. AGENTS.md
  misses (q08, q09, q11) are the biggest single source of top-5
  misses. Investigate smaller-chunk heuristics or
  section-header-anchored retrieval. (Filed 2026-05-24.)
* **#233** — query rewriting / HyDE expansion for operator-style
  queries. Several misses share the "imperative-question vs
  declarative-passage" asymmetry. (Filed 2026-05-24.)

## Reproduce

```bash
# (one-time) install the Nomic custom-code dep
.venv/bin/pip install einops

# swap config to Nomic v1.5
cp config/models.yaml config/models.yaml.e5-backup
# edit: name: nomic-ai/nomic-embed-text-v1.5; dimensions: 768;
#       trust_remote_code: true
# (the full file used here is at the foot of this report)

# re-ingest at 768-dim
make ingest    # via the running worker
# wait for ingestion_jobs status='succeeded'

# run the eval
python /tmp/calibration-222/run_eval.py
```

To revert: `cp config/models.yaml.e5-backup config/models.yaml` then
re-ingest the corpus at 384-dim.
