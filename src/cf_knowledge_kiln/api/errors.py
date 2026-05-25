"""Structured error envelope (#258).

Every error response from the kiln returns the same shape:

```json
{
  "error_code": "embedding_unavailable",
  "message": "...",
  "retry_safe": true,
  "retry_after_seconds": 30,
  "request_id": "abc-123",
  "detail": {}
}
```

Why this matters: agents consuming the API need a stable
machine-readable code to switch on. Pre-#258 every error path raised
``HTTPException(detail='free-form prose')``; agents that wanted to
distinguish 'token expired' from 'rate limited' from 'DB down' had
to substring-match prose. The envelope gives them an enum.

The envelope also closes the observability loop with #256 + #260:

* ``request_id`` matches the ``X-Request-ID`` header on the
  response, the per-request log line, and (once the telemetry
  migration lands) the rag_queries / rag_answers / context_packs
  audit row keyed on the request.
* When a user complaint quotes an error_code + request_id, an
  operator has every key needed to reconstruct what happened.

Wiring lives in :mod:`cf_knowledge_kiln.api.app` via three
``app.exception_handler`` registrations:

* :class:`HTTPException` → map status_code → error_code, preserve
  caller-set headers (Retry-After), wrap the detail prose
* :class:`pydantic.ValidationError` (FastAPI raises ``RequestValidationError``)
  → map to ``invalid_request`` with the Pydantic errors in ``detail``
* Generic ``Exception`` → ``internal_error``, log the traceback,
  return a stable shape without leaking implementation detail
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ErrorCode = Literal[
    # Client errors (4xx)
    "auth_required",
    "invalid_request",
    "query_too_long",
    "invalid_filter_value",
    "token_budget_too_low",
    "rate_limited",
    # Server errors (5xx)
    "db_unreachable",
    "embedding_unavailable",
    "generator_unavailable",
    "internal_error",
]
"""Closed set of error codes. Agents switch on these.

When adding a code: bump the OpenAPI enum + add a handler-side
mapping + cover with a unit test. Never silently widen the set."""


class ErrorResponse(BaseModel):
    """Stable error shape — same JSON for every non-2xx response.

    ``message`` is human-readable; ``error_code`` is the machine
    switch. ``retry_safe`` tells an agent whether retrying makes
    sense. ``retry_after_seconds`` is set when ``retry_safe`` is
    true AND we have a concrete delay (rate limit, transient
    upstream); ``None`` means "we don't know, back off your way."

    ``request_id`` matches the ``X-Request-ID`` response header and
    the per-request log line (#260) — operators ask the user to
    quote this when filing a complaint.

    ``detail`` is a free-form bag for context (the Pydantic error
    list on a 422, the rate-limit bucket name on a 429). Stable for
    the same error_code; never required to be parsed.
    """

    model_config = ConfigDict(extra="forbid")

    error_code: ErrorCode
    message: str
    retry_safe: bool = False
    retry_after_seconds: int | None = None
    request_id: str | None = None
    detail: dict[str, Any] | None = None


# Status-code → (error_code, retry_safe, default_retry_after_seconds)
# fallback map for HTTPExceptions raised with a status but no explicit
# error_code. Concrete dependencies (api/dependencies.py, api/auth.py,
# api/rate_limit.py) override these via :func:`raise_with_code` so the
# important codes get specific values, but a stray ``raise
# HTTPException(status_code=503, detail='...')`` still produces a
# clean envelope from this table.
_STATUS_DEFAULTS: dict[int, tuple[ErrorCode, bool, int | None]] = {
    400: ("invalid_request", False, None),
    401: ("auth_required", False, None),
    403: ("auth_required", False, None),  # treated same shape; agent retries with new creds
    404: ("invalid_request", False, None),
    413: ("query_too_long", False, None),  # POST /search uses 413 for over-MAX_QUERY_LENGTH
    422: ("invalid_request", False, None),
    429: ("rate_limited", True, None),  # Retry-After header from caller takes precedence
    500: ("internal_error", False, None),
    503: ("db_unreachable", True, 30),  # most 503s are transient; 30s is the floor
}


def default_for_status(status_code: int) -> tuple[ErrorCode, bool, int | None]:
    """Lookup the fallback error_code + retry shape for a bare HTTPException.

    Returns ``("internal_error", False, None)`` for unmapped status
    codes — the generic-exception shape. Callers that have a more
    specific code use :func:`raise_with_code` instead of relying on
    this fallback.
    """
    return _STATUS_DEFAULTS.get(status_code, ("internal_error", False, None))


__all__ = ["ErrorCode", "ErrorResponse", "default_for_status"]
