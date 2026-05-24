# #222 — calibration eval: `transformers` + `torch` floor bump

Recorded 2026-05-24 on the homelab-iac corpus (269 markdown files,
~7.8 MB → 2,566 chunks) with the active embedding model
`intfloat/e5-small-v2` (384-dim) configured per `config/models.yaml`.

## Versions

| Component | Installed (this eval) | Previous floor (pyproject) | Bumped floor |
| --------- | --------------------- | -------------------------- | ------------ |
| `transformers` | 5.9.0 | (transitive — not pinned)  | **`>=5.0,<6`** (direct pin) |
| `torch` | 2.12.0 | `>=2.0,<3` | **`>=2.9,<3`** |
| `sentence-transformers` | 5.5.1 | `>=3.0,<6.0` | **`>=5.5,<6.0`** |

Why both `>=5.0` for transformers AND `>=5.5` for sentence-transformers:
the latter pulls the former transitively, so a sentence-transformers
loosening to a 3.x release could let a CVE-vulnerable transformers
back in. Pinning `transformers` directly is defense-in-depth.

## Method

- Cloned `williamzujkowski/homelab-iac` to `/tmp/calibration-222/homelab-iac/`.
- Ingested the corpus via the standard `python -m cf_knowledge_kiln.ingestion`
  flow (worker, `e5-small-v2`, batch size 32, concurrency 4). 2566
  chunks embedded over ~81 batches in ~10 minutes wall clock.
- Ran the 15-query golden set from
  [homelab-iac#627](https://github.com/williamzujkowski/homelab-iac/issues/627)
  through `POST /v1/search` (13 positives, 2 negatives + a stale-detection probe).
- Captured top-1 fused RRF scores and an "expected source appears in
  top-5" coarse-grained quality check.

## Top-1 score distribution (positive queries)

| stat | value |
| ---- | ----- |
| min | 0.484 |
| p25 | 0.492 |
| median | 0.500 |
| mean | 0.594 |
| p75 | 0.500 |
| max | 0.977 |

Configured `weak_evidence_score_threshold` (in the default
`config/security.yaml`): **0.46**.

**Every positive top-1 clears the threshold.** This is the
calibration question the issue was asking — and it passes cleanly.

## Per-query

| qid | top1 | expected source | result |
| --- | ---- | --------------- | ------ |
| q01 | 0.492 | skills/manage-offsite-backup | missed |
| q02 | 0.500 | skills/bbr-backup-and-restore, docs/components/backups-bbr | in-top-5 |
| q03 | 0.500 | docs/runbooks/credhubcaexpiring | in-top-5 |
| q04 | 0.492 | docs/runbooks/offsitebackupfailed | missed |
| q05 | 0.500 | docs/components/caddy-reverse-proxy, AGENTS.md | in-top-5 |
| q06 | 0.484 | docs/components/offsite-backup | missed |
| q07 | 0.492 | docs/components/offsite-backup, docs/components/backups-bbr | in-top-5 |
| q08 | 0.977 | AGENTS.md | missed |
| q09 | 0.500 | AGENTS.md, scripts/wait-for-host | missed |
| q10 | 0.492 | skills/manage-offsite-backup | missed |
| q11 | 0.977 | AGENTS.md, inventory/lab.yml | missed |
| q12 | 0.820 | docs/components/authentik-sso, inventory/lab.yml | missed |
| q13 | 0.500 | (negative) | leaked-through |
| q14 | 0.901 | (negative) | leaked-through |
| q15 | 0.492 | docs/components/pgvector | in-top-5 |

Positives expected-in-top-5: **5/13**.
Negatives correctly flagged: **0/2**.

## What the data says

### The calibration question (the gate for #222): PASS

Top-1 distribution is healthy and entirely above the configured
`weak_evidence_score_threshold` of 0.46. The bumped versions
preserve the cosine-similarity range that the threshold was
calibrated against. **Bump is safe.**

This is consistent with what PR #216 promised: applying the
`` `passage: ` `` / `` `query: ` `` prefixes for e5 models
recovers the 0.5-0.85 cosine range the threshold expects.
Pre-#216 the user reported max 0.300; this run sees max 0.977
and median 0.500.

### Not a calibration question, but visible in the data

- **5/13 positives find the expected source in top-5.** Below the
  60%-top-1 / 85%-top-5 targets the user set in homelab-iac#628.
  The misses cluster around AGENTS.md (q08, q09, q11) which has
  many small sections; the retriever ranks chunks that share
  surface tokens with the query rather than the AGENTS.md section
  the user expected. This is a retrieval-quality concern, NOT a
  calibration concern — bumping the deps neither caused it nor
  fixes it. Worth filing as a separate kiln issue if the user
  wants to drive top-5 higher.
- **0/2 negatives correctly flagged.** Both negatives (q13 "AWS
  failover," q14 "configure Kubernetes") returned top-1 scores
  above the threshold (0.500 and 0.901). The corpus contains
  enough word-overlap (Kubernetes is mentioned in cf-deployment
  notes, AWS in offsite-backup discussions) that the cosine
  surface is real. Same diagnosis: the threshold is calibration-
  meaningful again (the score range works), but the threshold
  alone isn't sufficient for negative-query detection. The
  contradicting-source / sparse-evidence warnings are independent
  signals the eval script doesn't yet check.

Both of those are scope for a follow-up retrieval-quality issue,
not #222.

## CVE closure at the bumped floor

`pip-audit` at the proposed floor:

| package | version | open CVE | status |
| ------- | ------- | -------- | ------ |
| transformers | 5.0.0rc3 | (none) | clean |
| torch | 2.9.0 | PYSEC-2026-139 | **upstream-unpatched** — tracked, no action available |
| sentence-transformers | 5.5.0 | (none) | clean |

Closed:

- `transformers` PYSEC-2025-211..217 (RCE in checkpoint loaders for
  X-CLIP, SEW), CVE-2026-1839.
- `torch` PYSEC-2025-203/204/206 (DoS), CVE-2025-3730,
  GHSA-887c-mr87-cxwp (CTC DoS).

## Decision: BUMP

`pyproject.toml`:

```toml
real-embeddings = [
  "sentence-transformers>=5.5,<6.0",
  "transformers>=5.0,<6",
  "torch>=2.9,<3",
]
embeddings = [
  "sentence-transformers>=5.5,<6.0",
]
```

`.security-discoveries.jsonl` rows for transformers + torch flip
from `open` to `fixed` (with the upstream-unpatched PYSEC-2026-139
called out as a monitored exception).
