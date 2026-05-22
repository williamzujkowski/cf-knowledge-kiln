# Relevance grading — review_precision.yaml

`review_precision.yaml` carries a `relevance:` map per case — a 0-3
relevance grade for each chunk the retriever actually returns. The
per-bucket confidence-precision test (`test_review_precision.py`,
`KILN_EVAL_REAL_EMBEDDINGS=1`) consumes these grades.

Grading is a judgment task, so it is not fully automatable. This
document + `tests/eval/regrade_review_precision.py` make it
**reproducible**: the mechanical steps are scripted and deterministic,
and the one judgment step produces committable artifacts.

## Rubric

| Grade | Meaning |
| ----- | ------- |
| 3 | perfect answer to the query |
| 2 | useful, partial |
| 1 | tangentially relevant |
| 0 | irrelevant |

Grade the chunk against the **query**, not the case's expected outcome.

## When to re-grade

Re-grade when the inputs the grades describe change:

- the eval corpus (`docs/_eval/`) gains, loses, or edits a document;
- the retriever's top-K behaviour shifts (a ranking/scoring change that
  moves which chunks land in the top 3) — e.g. an embedding-model swap,
  an RRF change, a re-baselined threshold.

A grade is only valid for the (query, chunk) pair it was assigned to.
If a chunk falls out of the top 3, its grade is stale.

## Procedure

1. **Generate the worksheet** (deterministic — needs the DB + the
   `real-embeddings` extra):

   ```bash
   KILN_DATABASE_URL=postgresql+asyncpg://kiln:kiln@localhost:5432/kiln \
     python -m tests.eval.regrade_review_precision worksheet
   ```

   This seeds `docs/_eval/`, runs the real-embedding retriever over
   every review case, and writes `tests/eval/reports/grading_worksheet.yaml`
   — each case's query plus its top-3 chunks (citation + text excerpt).
   `tests/eval/reports/` is gitignored; the worksheet is regenerable.

2. **Grade independently.** Dispatch the worksheet to **at least 3
   independent judges** (separate subagents, or people) — independence
   is what makes the median meaningful. Each judge produces a YAML
   grade file:

   ```yaml
   # judge_1.yaml — { case_id: { citation: grade } }
   conflict-token-rotation:
     "kiln-eval/auth-policy-current.md#Bearer token rotation policy": 3
     "kiln-eval/auth-policy-legacy.md#Bearer token rotation policy": 3
   ```

   Citations are copied verbatim from the worksheet (they are the
   keys `review_precision.yaml` uses). Commit the judge files under
   `tests/eval/golden/grading/` so the median is reproducible from the
   committed artifacts.

3. **Aggregate** (deterministic):

   ```bash
   python -m tests.eval.regrade_review_precision aggregate \
     tests/eval/golden/grading/judge_*.yaml
   ```

   This prints the per-pair median grade + a consensus summary
   (`N/M unanimous, K/M with disagreement spread >= 2`). A wide spread
   (>= 2) on many pairs means the rubric or the judges disagree — worth
   a look before trusting the median.

4. **Update** `review_precision.yaml` — paste the rendered `relevance:`
   blocks into the matching cases, refresh the consensus marker
   comment, then re-run the calibration eval:

   ```bash
   KILN_EVAL_REAL_EMBEDDINGS=1 make eval
   ```

   If `test_confidence_buckets_meet_per_bucket_precision` shifts, that
   is real signal — re-measure and, if warranted, adjust
   `_PER_BUCKET_PRECISION_FLOOR` (see `_review_precision_helpers.py`).

## History

The grades currently in `review_precision.yaml` predate this harness —
they came from a one-off 5-judge fan-out (consensus was strong: 26/33
unanimous, 0/33 with spread >= 2). Their raw per-judge artifacts were
not retained. The harness above governs every grading from now on, and
each run leaves committed judge files.
