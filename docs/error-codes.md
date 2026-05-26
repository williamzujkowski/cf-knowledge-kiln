# Error codes

The kiln returns a stable `ErrorResponse` envelope for every non-2xx response (PR [#266](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/266)). Every documented response on a protected operation `$ref`s this shape (PR [#299](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/299)).

This page is the canonical reference: the closed-set `error_code` enum, what each code means, when it fires, what an operator does, and the contract for adding a new code.

> The OpenAPI schema in [`openapi/openapi.yaml`](../openapi/openapi.yaml) is the wire contract; this doc is the prose explanation. The enum is also defined in code at [`src/cf_knowledge_kiln/api/errors.py`](../src/cf_knowledge_kiln/api/errors.py).
>
> For the per-complaint audit recipe (request_id → telemetry row → chunks), see [runbooks/audit-trail.md](./runbooks/audit-trail.md). For the agent integration story (retry policy, `X-Request-ID` honoring), see [agent-integration-guide.md §6](./agent-integration-guide.md#error-handling).

---

## The envelope

```json
{
  "error_code": "rate_limited",
  "message": "Per-IP rate limit exceeded.",
  "retry_safe": true,
  "retry_after_seconds": 47,
  "request_id": "4b3f1c8e-0d2a-4f3e-9c1b-7a5b8e2d4f6a",
  "detail": null
}
```

| Field | Stability | What it carries |
| --- | --- | --- |
| `error_code` | **Stable closed set.** Bumping the enum is a semver-major signal. | The machine-readable code agents switch on. |
| `message` | Not stable. Human prose. | Why it happened; never required to parse. |
| `retry_safe` | Stable. | `true` when retrying the same request might succeed (transient upstream); `false` when an operator must intervene (auth, malformed request). |
| `retry_after_seconds` | Stable. | Concrete delay when known (mirrors `Retry-After` header for HTTP-aware clients). `null` when `retry_safe: true` but no specific delay known. |
| `request_id` | Stable. | Matches the `X-Request-ID` response header + the per-request log line + the telemetry row. Quote this in user complaints. |
| `detail` | Stable per-`error_code` shape. | Free-form context bag. For `invalid_request` from Pydantic, carries `{"errors": [...]}` with per-field validation errors. |

---

## The closed-set enum

### Client errors (4xx)

#### `auth_required`

- **Status:** 401 (also returned as 403 → the same `error_code` shape; the kiln treats both as "fix your credentials and retry")
- **Retry-safe:** No
- **When it fires:** Missing or invalid `Authorization: Bearer <token>` while `KILN_AUTH_MODE=bearer`.
- **Detail shape:** `null`.
- **Operator action:** Rotate / repair the bearer token, then retry. The `WWW-Authenticate` header carries the realm.

#### `invalid_request`

- **Status:** 400, 404, or 422 (the default for unmapped 4xx)
- **Retry-safe:** No
- **When it fires:** Pydantic validation failed (422 with `detail.errors`), or a path/method-route mismatch (404), or a generic 400.
- **Detail shape:** On 422 from Pydantic: `{"errors": [{"loc": [...], "msg": "...", "type": "..."}]}` — FastAPI's per-field error list. Otherwise `null`.
- **Operator action:** Fix the request body; check `detail.errors` for the failing field.

#### `query_too_long`

- **Status:** 400 (HTMX `POST /search` uses 413 with this code)
- **Retry-safe:** No
- **When it fires:** Query string exceeded `MAX_QUERY_LENGTH` (4096 chars) — guards the FTS + embedding compute cost.
- **Operator action:** Shorten the query.

#### `invalid_filter_value`

- **Status:** 400
- **Retry-safe:** No
- **When it fires:** A filter value (status, doc_type, etc.) is outside the kiln's accepted enum.
- **Operator action:** Check the filter registry (planned `GET /v1/agent/filters` endpoint under epic #269); for now, see the values listed in [user-journeys.md](./user-journeys.md#filters).

#### `token_budget_too_low`

- **Status:** 400
- **Retry-safe:** No
- **When it fires:** `/v1/agent/context-pack` got a `max_tokens` so small that not even one chunk fits after metadata overhead.
- **Operator action:** Increase `max_tokens` (rule of thumb: at least 500 per chunk).

#### `rate_limited`

- **Status:** 429
- **Retry-safe:** **Yes.**
- **When it fires:** Per-IP token bucket exhausted (default 60/min for `/v1/search` + `/v1/agent/context-pack` + `/v1/answer`; 30/min for `/feedback`).
- **Detail shape:** `null` (the bucket name isn't surfaced — it'd let a probing client map the limit topology).
- **Operator action:** Wait `retry_after_seconds` (also in the `Retry-After` header). Exponential back-off on repeated 429s.

### Server errors (5xx)

#### `db_unreachable`

- **Status:** 503
- **Retry-safe:** **Yes.** Default `retry_after_seconds: 30`.
- **When it fires:** Postgres connection fails (typical transient cause: CF service binding churn, network blip, DB rolling restart).
- **Operator action:** Retry after the delay. If persistent, check `/readyz` for the rolled-up health.

#### `embedding_unavailable`

- **Status:** 503
- **Retry-safe:** **Yes.**
- **When it fires:** Embedding provider (Nomic, OpenAI-compatible) returned an error or timed out.
- **Operator action:** Retry. The hybrid retriever falls back to FTS-only on this path internally when possible; a 503 with this code means even that wasn't enough.

#### `generator_unavailable`

- **Status:** 503
- **Retry-safe:** **No.** (Configuration issue, not transient.)
- **When it fires:** `/v1/answer` was called but no generator is configured (the MVP default — operators bring it up by setting `KILN_GENERATOR_*` env vars).
- **Operator action:** Either enable a generator (`config/models.yaml`'s `models.generator` block + `KILN_GENERATOR_BASE_URL` / `KILN_GENERATOR_API_KEY`) or switch the caller to `/v1/agent/context-pack` and synthesize on the agent side.

#### `internal_error`

- **Status:** 500
- **Retry-safe:** No
- **When it fires:** An unhandled exception escaped to the global handler. Always logged with traceback at server-side ERROR level.
- **Detail shape:** `null` (never leaks implementation detail to the wire).
- **Operator action:** Capture `request_id`; trace through `runbooks/audit-trail.md` to the log line and root-cause. File an issue with the request_id and the timeline.

---

## Status → `error_code` fallback map

When a `raise HTTPException(status_code=...)` slips through without an explicit code, the global handler ([`api/error_handlers.py`](../src/cf_knowledge_kiln/api/error_handlers.py)) falls back to this map (defined in `api/errors.py:_STATUS_DEFAULTS`):

| Status | Fallback `error_code` | `retry_safe` | Default delay |
| --- | --- | --- | --- |
| 400 | `invalid_request` | false | — |
| 401 | `auth_required` | false | — |
| 403 | `auth_required` | false | — |
| 404 | `invalid_request` | false | — |
| 413 | `query_too_long` | false | — |
| 422 | `invalid_request` | false | — |
| 429 | `rate_limited` | true | — (Retry-After from caller wins) |
| 500 | `internal_error` | false | — |
| 503 | `db_unreachable` | true | 30s |

Anything outside this table falls to `("internal_error", false, None)` — the generic-exception shape.

---

## Adding a new code

The enum is closed by design. Adding a code is a deliberate API change:

1. **Add the code to `ErrorCode` Literal** in [`src/cf_knowledge_kiln/api/errors.py`](../src/cf_knowledge_kiln/api/errors.py) and re-export from `__all__` if applicable.
2. **Bump the OpenAPI enum** in [`openapi/openapi.yaml`](../openapi/openapi.yaml) (under `components.schemas.ErrorResponse.properties.error_code.enum`). The drift test in [`tests/unit/test_openapi_security_and_errors.py`](../tests/unit/test_openapi_security_and_errors.py) will fail otherwise.
3. **Add the handler mapping** in [`src/cf_knowledge_kiln/api/error_handlers.py`](../src/cf_knowledge_kiln/api/error_handlers.py). For status-fallback cases, extend `_STATUS_DEFAULTS` in `errors.py`.
4. **Document it here** — add a new subsection under the appropriate 4xx / 5xx group with the same shape as the others (status / retry-safe / when / detail / operator action).
5. **Cover with a unit test** that asserts the envelope shape on the trigger path.
6. **Re-render the agent-integration guide** table in [agent-integration-guide.md §6](./agent-integration-guide.md#error-handling) if this code reaches `/v1/agent/*` consumers.

The semver implication: adding an enum value is **backward-compatible for consumers that switch on `error_code` with a default branch**. Consumers that exhaustively pattern-match (Rust `match`, Python's `match`+`case` with no wildcard) will need to update. Document any addition in the PR description as "API additive" so downstream agents can grep their releases.

Removing or renaming a code is a **semver-major** change and goes through the API compatibility policy (planned ADR-0012 under epic [#270](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/270)).

---

## See also

- [`agent-integration-guide.md §6`](./agent-integration-guide.md#error-handling) — retry policy + `X-Request-ID` honoring (consumer perspective).
- [`runbooks/audit-trail.md`](./runbooks/audit-trail.md) — `request_id` → telemetry row → evidence chunks (operator perspective).
- [`openapi/openapi.yaml`](../openapi/openapi.yaml) `components.schemas.ErrorResponse` — the wire contract.
- [`src/cf_knowledge_kiln/api/errors.py`](../src/cf_knowledge_kiln/api/errors.py) — the source of truth in code.
