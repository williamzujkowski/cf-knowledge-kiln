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

Percentile convention used here (same as the e5 baseline report and
`tests/eval/run_calibration_eval.py`): floor-index pick,
`sorted[(3*n)//4]` with n=13 → index 9 → 0.500. (Linear-interpolation
`numpy.percentile(..., 75)` gives the same answer for this data;
linear-rank-with-1-indexed-positions gives 0.868. The floor-index
choice matches across both calibration reports so they're comparable.)

Score *shape* is essentially identical. The fused-RRF normalization
(#164) caps both arms-rank-1 at 1.0 and single-arm rank-1 at 0.5; both
models land most positives at the 0.5 single-arm plateau, with a few
both-arm hits up at 0.97.

## Quality metrics

| metric | e5-small-v2 | nomic-v1.5 |
| ------ | ----------- | ---------- |
| positive top-1 ≥ weak_evidence floor (0.46) | 13/13 (100%) | 13/13 (100%) |
| positive top-5 hit | **5/13 (38%)** | **4/13 (31%)** |
| negative correctly flagged | 0/2 (0%) | 0/2 (0%) |
| user's target | 85% top-5 | (no change) |

The weak-evidence floor row counts positive queries only — the two
negatives (q13, q14) also score at 0.500 on nomic (and 0.500 / 0.901
on e5), so the gate doesn't catch them either way. That's the
motivation for #227 isolated_match and the rest of the rejection
discussion below.

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
# (one-time) install the Nomic custom-code dep (see #231)
.venv/bin/pip install einops

# swap config to Nomic v1.5
cp config/models.yaml config/models.yaml.e5-backup
# edit: name: nomic-ai/nomic-embed-text-v1.5; dimensions: 768;
#       trust_remote_code: true

# re-ingest at 768-dim (worker uses the new provider on restart)
make ingest    # enqueue + drain via the running worker
# wait for ingestion_jobs.status='succeeded'

# run the eval — script is committed to the repo (#228 follow-up)
python tests/eval/run_calibration_eval.py > /tmp/my-nomic-results.md

# revert + clean up
cp config/models.yaml.e5-backup config/models.yaml
rm config/models.yaml.e5-backup
# re-ingest at 384-dim to repopulate the DB with e5 vectors
make ingest
```

Note on q14 top-2 (#227 calibration claim): the report says
isolated_match's gap is 0 on nomic-v1.5 q14 because top-1 is at the
0.500 single-arm plateau and the corpus has multiple chunks that also
land at 0.500 on this query — so the gap to top-2 is at most a small
fraction of the score range, well below the 0.30 drop_threshold. This
is consistent with the score-distribution shape but isn't separately
audited by the eval script (which only captures top-1). A future
revision of the script could log top-2 explicitly to make this claim
self-verifying.

## Postscript (2026-05-24, post-merge): The misses aren't all retrieval problems

A read-only diagnostic against the live DB (with the e5 corpus
re-ingested) revealed that most "top-5 misses" in this report are
not retrieval-quality issues at all. They split into three buckets:

| qid | expected target | actual category |
| --- | --------------- | --------------- |
| q01 | `skills/manage-offsite-backup` | **0 chunks** — path does not exist in this corpus snapshot |
| q04 | `docs/runbooks/offsitebackupfailed` | **0 chunks** — filename drift (corpus has `backupoffsitereplicationfailed.md`, not the expected name) |
| q06 | `docs/components/offsite-backup` | **0 chunks** — path does not exist (only `docs/proposals/178-offsite-backup-design.md` is present) |
| q08 | `AGENTS.md` | found at **rank 20** — real retrieval issue (chunk-ranking, not corpus) |
| q09 | `AGENTS.md`, `scripts/wait-for-host` | AGENTS.md present (23 chunks) but ranked beyond top-25; **`scripts/wait-for-host` is a shell script**, excluded by the `**/*.md` ingest filter |
| q10 | `skills/manage-offsite-backup` | **0 chunks** — same path as q01 |
| q11 | `AGENTS.md`, `inventory/lab.yml` | AGENTS.md found at rank 19 (real retrieval issue); **`inventory/lab.yml` is YAML**, excluded by the `**/*.md` filter |
| q12 | `docs/components/authentik-sso`, `inventory/lab.yml` | authentik-sso has 10 chunks but ranked beyond top-25 (real retrieval issue); lab.yml is YAML (filter) |

Of the 8 missed positive queries, **at least 5 were corpus problems,
not retrieval problems**:

- **3 expected paths don't exist at all** (q01, q06, q10 → 
  `skills/manage-offsite-backup`, `docs/components/offsite-backup`)
- **1 filename drift** (q04: corpus has the alert renamed to
  `backupoffsitereplicationfailed`)
- **2 non-markdown expected files** that the ingest filter
  legitimately excludes (q09 shell script, q11+q12 YAML inventory)

The **truly retrieval-quality problems** are q08, q09 (AGENTS.md
side), q11 (AGENTS.md side), and q12 (authentik-sso) — chunks exist
in the DB but rank beyond top-25 on these queries. That's still a
real signal for #232 (long-section AGENTS.md chunking) and #233
(query rewriting), but the proportion is smaller than the headline
"5/13 top-5" suggested.

### Headline-metric corrected

| metric | as reported | as observed |
| ------ | ----------- | ----------- |
| total positive queries | 13 | 13 |
| retriever's fault | 8 misses | **3-4 misses** (q08, q11, q12; q09 partially) |
| corpus-drift / out-of-scope-file-type | 0 (not separated) | **4-5 misses** (q01, q04, q06, q10, q11/q12/q09 file-type pieces) |

### Implications for the follow-ups

* **#232 — chunking for long-section docs**: still real (AGENTS.md
  ranks at 19-20 on multiple queries; authentik-sso has 10 chunks
  none of which surface). But the headline lever is smaller —
  shrinking max_tokens won't help q01/q04/q06/q10 at all.
* **#233 — query rewriting**: weaker case than originally framed.
  The "operator-imperative vs declarative-passage" hypothesis can't
  be validated against queries whose expected target doesn't exist.
  Worth re-scoping to the queries with real retrieval-quality
  issues (q08, q09, q11, q12) and re-evaluating the asymmetry.
* **homelab-iac side**: the golden set in homelab-iac#627 needs
  reconciliation with the current corpus state — either the missing
  paths get created, OR the golden set is updated to target files
  that exist. Tracked as a comment on #232.

Closing thought: this is a textbook example of why benchmark
diagnostics need to look at WHAT the retriever returned and WHERE
the expected target landed, not just the headline hit-rate. The
#228 report's headline framing ("Nomic v1.5 doesn't lift the gap")
remains correct — neither embedder can find files that aren't in
the DB — but the framing of "retrieval-quality gap" was too broad.
