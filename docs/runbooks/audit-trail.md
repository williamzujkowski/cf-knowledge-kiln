# Runbook: audit-trail lookups by `request_id`

**Scope:** an operator gets a user complaint quoting a `request_id`,
`context_pack_id`, `answer_id`, or `error_code`. This runbook walks
the SQL + log queries that reconstruct what the user saw — what
chunks were retrieved, what the generator was fed, what envelope
the agent received.

The wiring this depends on:

- **`X-Request-ID` middleware** (PR [#265](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/265))
  generates or honors a per-request correlation key, exposes it on
  `request.state.request_id`, echoes it on the response header, and
  stamps it on the per-request log line.
- **Wire-id persistence** (PR [#256](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/256))
  uses the response's `context_pack_id` / `answer_id` as the row PK
  on `context_packs` / `rag_answers` so the wire-visible UUID is
  also the DB lookup key.
- **`request_id` columns** (PR [#272](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/272))
  persist the correlation key on all three telemetry tables
  (`rag_queries`, `rag_answers`, `context_packs`) with partial
  indexes for fast lookup.

---

## 1. What the user gives you

Any one of these is enough to start:

| Handle              | Where the user sees it                                                                  |
| ------------------- | --------------------------------------------------------------------------------------- |
| `request_id`        | `X-Request-ID` response header; `request_id` field in any error envelope; some app logs |
| `context_pack_id`   | Body of a successful `/v1/agent/context-pack` response                                  |
| `answer_id`         | Body of a successful `/v1/answer` response                                              |
| `error_code` + time | Body of any error envelope, plus the timestamp the user remembers                       |

If the user only has the rough time of the failure, start from CF
logs and narrow to a `request_id` first.

---

## 2. From `request_id` → the log line

The per-request log line stamps `request_id` so you can grep across
the platform's log surface:

```bash
# Cloud Foundry — last 1000 lines of the API app
cf logs cf-knowledge-kiln-api --recent | grep 'request_id=req_abc'

# Cloud Foundry — also check the worker (ingestion telemetry doesn't
# use request_id today, but worker failures sometimes correlate with
# the same user-visible window).
cf logs cf-knowledge-kiln-worker --recent | grep 'request_id=req_abc'
```

If you self-host, swap the `cf logs` invocation for whatever your
log aggregator exposes. The middleware sanitizes inbound values
(see `src/cf_knowledge_kiln/api/request_id.py:_sanitize` — alphanumerics
plus `._-`, max 200 chars) so any value you got from the user that
matches that shape is safe to grep verbatim.

The log line tells you the route, status code, and (for 5xx) the
error envelope's `error_code`. That's enough to pick which telemetry
table to query next.

---

## 3. From `request_id` → the telemetry row

Three tables carry `request_id`, indexed for the lookup. Pick by
endpoint:

| Route                       | Table             | What the row carries                                                                                                |
| --------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/search`           | `rag_queries`     | `query`, `filters` (JSONB), `retrieved_chunk_ids[]`, `consumer_type='human'`                                        |
| `POST /v1/agent/context-pack` | `context_packs` | `query`, `task`, `filters`, `evidence_chunk_ids[]`, `token_budget`, `confidence`, `warnings`, `requires_human_review` |
| `POST /v1/answer`           | `rag_answers`     | `query`, `task`, `filters`, `evidence_chunk_ids[]`, `answerable`, `refusal_reason`, `generator_provider/model`, token counts |
| `POST /search` (human HTMX) | `rag_queries`     | same shape as `/v1/search`, `consumer_type='human'`                                                                 |

### Lookup by `request_id`

```sql
-- Pick the right table for the route. Same shape everywhere.
SELECT * FROM rag_queries     WHERE request_id = 'req_abc' ORDER BY created_at DESC LIMIT 5;
SELECT * FROM context_packs   WHERE request_id = 'req_abc' ORDER BY created_at DESC LIMIT 5;
SELECT * FROM rag_answers     WHERE request_id = 'req_abc' ORDER BY created_at DESC LIMIT 5;
```

A retry can produce multiple rows with the same `request_id` (the
agent retried on a transient failure; the middleware honored the
inbound header on each attempt). The `created_at` ordering shows
the sequence; the last row is typically the one the user saw.

### Lookup by wire id (no `request_id` available)

```sql
-- context_packs.id == the context_pack_id the agent received
SELECT * FROM context_packs WHERE id = 'a1b2c3d4-...';

-- rag_answers.id == the answer_id the agent received
SELECT * FROM rag_answers WHERE id = 'a1b2c3d4-...';

-- rag_queries doesn't expose its id on the wire; lookup needs
-- request_id or a (query, created_at, requester) tuple.
```

---

## 4. From the row → the evidence chunks

The `retrieved_chunk_ids` / `evidence_chunk_ids` array on the row
points to `document_chunks.id`. Join through:

```sql
-- Replace the WHERE clause with whichever lookup you used above.
WITH target AS (
  SELECT evidence_chunk_ids FROM context_packs WHERE request_id = 'req_abc'
  ORDER BY created_at DESC LIMIT 1
)
SELECT
  c.id            AS chunk_id,
  c.content,
  c.heading_path,
  d.repo,
  d.path,
  d.title,
  d.status,
  d.last_reviewed,
  d.commit_sha
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.id = ANY ((SELECT evidence_chunk_ids FROM target));
```

For the answer endpoint, swap `context_packs` for `rag_answers`. For
`/v1/search`, swap to `rag_queries.retrieved_chunk_ids`.

### What this gives you

For each chunk the agent saw at request time:

- **`content`** — the verbatim text the generator was fed. Compare
  to the user's complaint; the "bad answer" usually reduces to "the
  chunk content is misleading," "the chunk is from a deprecated
  doc the engine should have demoted," or "the chunk doesn't
  actually contain the claim the answer made (a grounding failure
  — file an issue)."
- **`status`** — was a deprecated/archived chunk in the evidence?
  The retrieval engine status-weights it down but doesn't exclude
  by default. If a deprecated chunk reached the generator and the
  answer cites it as current, that's a treatment-policy bug.
- **`last_reviewed`** — staleness. Combine with the warning emitted
  on the response (`stale_source`) for the audit trail.
- **`commit_sha`** — the snapshot of the document that was indexed.
  Use this with `repo` + `path` to retrieve the exact file content
  the ingester saw, in case the upstream doc has since changed.

---

## 5. Worked example — bad-answer complaint

> "Your agent told me to run `gcloud beta cf foo bar` and it doesn't
> exist. Here's my `answer_id: a1b2c3d4-1111-2222-3333-444455556666`."

```sql
-- 1. Pull the answer row.
SELECT id, request_id, query, task, evidence_chunk_ids,
       generator_model, finish_reason, refusal_reason,
       answerable, requires_human_review, created_at
FROM rag_answers
WHERE id = 'a1b2c3d4-1111-2222-3333-444455556666';
```

```text
 id            | a1b2c3d4-1111-2222-3333-444455556666
 request_id    | req_2026-05-26T03:14:15Z_x9y8z7
 query         | how do I redeploy a cf app
 task          | summarize redeploy steps
 evidence_chunk_ids | {ch-7..., ch-8..., ch-9...}
 generator_model    | gpt-5-mini
 finish_reason | stop
 refusal_reason     | null
 answerable    | t
 requires_human_review | f
 created_at    | 2026-05-26 03:14:16+00
```

```sql
-- 2. Pull the chunks the generator saw.
SELECT c.id, d.repo, d.path, d.status, c.heading_path,
       LEFT(c.content, 200) AS preview
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.id = ANY (
  ARRAY['ch-7...', 'ch-8...', 'ch-9...']::uuid[]
);
```

If one of the chunks comes back as `repo: 'random-fork', path:
'old-cli-notes.md', status: 'archived'` — the engine surfaced an
archived doc and the generator cited it as authoritative. That's a
double bug: (a) why did an archived chunk score high enough to
appear in evidence, and (b) why did the synthesis prompt not honor
the status warning. File both as separate issues; the chunk + doc
ids let the next reviewer reproduce without involving the user.

If all chunks are `status: 'active'` and the content really does
prescribe `gcloud beta cf foo bar` — the source doc is wrong.
Quarantine it (see the "quarantine a compromised source" runbook —
[#270](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/270))
and file an upstream PR against the indexed repo.

---

## 6. Lookup by `error_code` (no IDs at all)

If the user only has the error envelope:

```sql
-- error_code lives in the JSONB column on rag_queries.filters
-- or rag_answers.refusal_reason, depending on the failure path.
-- For 5xx envelopes there's no telemetry row — the handler exited
-- before the write. Use the log line as the primary source.

-- Recent rate-limit complaints (the user retried before hitting
-- you up):
SELECT request_id, query, created_at
FROM rag_queries
WHERE created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC
LIMIT 50;
```

The agent guide
[§6 `error_code` enum](../agent-integration-guide.md#error_code-enum)
documents which codes correspond to which class of failure;
combined with the time-window query above you can usually narrow
to the user's session.

---

## 7. What's NOT in the telemetry

A few things deliberately not stored — operator awareness, not gaps
to file:

- **Generator response body.** Token counts (`prompt_tokens`,
  `completion_tokens`, `total_tokens`) and the finish reason are
  on `rag_answers`, but not the synthesized answer text itself
  (privacy + storage cost). Reconstruct via the chunks + the
  prompt template (`src/cf_knowledge_kiln/agent/prompts.py`) if a
  reviewer needs to see what the generator was fed.
- **Bearer-token identity.** When `KILN_AUTH_MODE=bearer` the
  token validates the request but isn't joined to telemetry —
  multi-tenant identity is out of scope for the MVP per ADR-0011
  (planned).
- **Per-chunk score components.** The retrieval engine produces
  per-arm RRF + status weight + freshness factor for each chunk,
  but only the fused `score` (in the response) is implicit in
  the chunk's position in `evidence_chunk_ids`. Re-run the same
  query with `KILN_LOG_LEVEL=DEBUG` to surface the components in
  the engine logs.

---

## See also

- [agent-integration-guide.md §6](../agent-integration-guide.md#error-handling) — `error_code` enum + retry-policy contract
- [`docs/observability.md`](../observability.md) — log-line shape, OTLP wiring
- [`docs/troubleshooting.md`](../troubleshooting.md) — common failure modes + first-response checks
- ADR [`0010-five-channel-deprecation-signal.md`](../adr/0010-five-channel-deprecation-signal.md) — why an archived chunk in evidence is a double bug
