"""Global exception handlers that produce :class:`ErrorResponse`.

Registered in :func:`cf_knowledge_kiln.api.app.create_app` via
:func:`install_error_handlers`. Three handlers, narrowest first:

1. ``HTTPException`` — what most of the kiln raises today. Maps the
   status code → error_code (table in :mod:`api.errors`), preserves
   any caller-set headers (notably ``Retry-After`` from the rate
   limiter), and wraps ``detail`` into the structured shape.
2. ``RequestValidationError`` — Pydantic's "your request body is
   wrong" 422. Maps to ``invalid_request`` with the per-field errors
   in ``detail`` so an agent can locate the bad field.
3. ``Exception`` — last resort. Logs the full traceback with the
   request_id; returns ``internal_error`` to the client without
   leaking the traceback.
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cf_knowledge_kiln.api.errors import ErrorCode, ErrorResponse, default_for_status
from cf_knowledge_kiln.api.request_id import HEADER as REQUEST_ID_HEADER
from cf_knowledge_kiln.api.request_id import request_id_for

logger = logging.getLogger(__name__)


# Sentinel keys an upstream raiser can stash on HTTPException.detail
# (when detail is a dict) to override the status-code defaults. Lets
# a specific handler raise ``HTTPException(503, detail={"_error_code":
# "embedding_unavailable", "_message": "..."})`` to get the right
# code without a custom exception class.
_DETAIL_CODE_KEY = "_error_code"
_DETAIL_MESSAGE_KEY = "_message"
_DETAIL_RETRY_AFTER_KEY = "_retry_after_seconds"
# Sentinel — distinguishes "key not present" from "key present with
# value None" when reading the optional override.
_RETRY_AFTER_SENTINEL: Any = object()


def _envelope_from_http_exception(exc: HTTPException, request_id: str | None) -> ErrorResponse:
    """Build the envelope from an HTTPException, honoring explicit overrides."""
    code, retry_safe, retry_after = default_for_status(exc.status_code)
    detail_payload: dict[str, Any] | None = None
    message: str

    # detail can be: str (most common), dict (when caller wants to
    # surface structure), list (rare; Pydantic-style), or None.
    if isinstance(exc.detail, dict):
        # Extract override sentinels if present; pass the rest through.
        rest = dict(exc.detail)
        override_code = rest.pop(_DETAIL_CODE_KEY, None)
        override_message = rest.pop(_DETAIL_MESSAGE_KEY, None)
        override_retry_after = _RETRY_AFTER_SENTINEL
        if _DETAIL_RETRY_AFTER_KEY in rest:
            override_retry_after = rest.pop(_DETAIL_RETRY_AFTER_KEY)
        if override_code is not None:
            # Overriding the code overrides the whole row — operators
            # who want a custom retry shape say so explicitly via
            # raise_with_code(retry_after_seconds=...).
            code = override_code
            retry_safe = False
            retry_after = None
        if override_retry_after is not _RETRY_AFTER_SENTINEL:
            retry_after = override_retry_after
            retry_safe = retry_after is not None
        message = override_message or _default_message_for(code)
        detail_payload = rest or None
    elif isinstance(exc.detail, str):
        message = exc.detail or _default_message_for(code)
    else:
        message = _default_message_for(code)
        if exc.detail is not None:
            detail_payload = {"raw": exc.detail}

    # Retry-After response header (when present from the caller — e.g.
    # rate-limit) wins over the table default. We carry it onto the
    # response in :func:`install_error_handlers`; here we just compute
    # the envelope field.
    return ErrorResponse(
        error_code=code,
        message=message,
        retry_safe=retry_safe,
        retry_after_seconds=retry_after,
        request_id=request_id,
        detail=detail_payload,
    )


def _default_message_for(code: ErrorCode) -> str:
    """Human-readable fallback when the caller didn't supply prose."""
    return {
        "auth_required": "Authentication required.",
        "invalid_request": "The request was invalid.",
        "query_too_long": "Query exceeds the maximum length.",
        "invalid_filter_value": "One or more filter values were invalid.",
        "token_budget_too_low": "The requested token budget is too small.",  # nosec B105 — error message, not a credential; bandit pattern-matches on the substring 'token'
        "rate_limited": "Too many requests. Please slow down.",
        "db_unreachable": "Database is temporarily unavailable.",
        "embedding_unavailable": "Embedding provider is temporarily unavailable.",
        "generator_unavailable": "Generator is not configured or unavailable.",
        "internal_error": "An internal error occurred.",
    }[code]


def install_error_handlers(app: FastAPI) -> None:
    """Register the three global exception handlers on ``app``."""

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = request_id_for(request)
        envelope = _envelope_from_http_exception(exc, request_id)
        # Preserve caller-set headers (Retry-After from rate-limit,
        # WWW-Authenticate from auth) — the envelope adds structure
        # but doesn't override the HTTP-level signals.
        response_headers = dict(exc.headers or {})
        if request_id is not None and REQUEST_ID_HEADER not in response_headers:
            response_headers[REQUEST_ID_HEADER] = request_id
        # If the caller set Retry-After header AND we have a numeric
        # retry_after_seconds in the envelope, prefer the header value
        # in the envelope so the two match (rate-limit middleware
        # always sets the header).
        header_retry = response_headers.get("Retry-After")
        if header_retry is not None:
            try:
                envelope.retry_after_seconds = int(header_retry)
                envelope.retry_safe = True
            except (TypeError, ValueError):
                pass
        return JSONResponse(
            envelope.model_dump(exclude_none=False),
            status_code=exc.status_code,
            headers=response_headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = request_id_for(request)
        envelope = ErrorResponse(
            error_code="invalid_request",
            message="Request body validation failed.",
            retry_safe=False,
            request_id=request_id,
            detail={"errors": exc.errors()},
        )
        headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
        return JSONResponse(
            envelope.model_dump(exclude_none=False),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _generic_exc(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001 — exc is required by FastAPI's signature; the traceback comes from logger.exception
        request_id = request_id_for(request)
        # Log with the request_id so an operator can correlate this
        # 500 with the user's complaint (and with the per-request log
        # line emitted by api/request_log.py).
        logger.exception(
            "unhandled exception path=%s request_id=%s",
            request.url.path,
            request_id or "-",
        )
        envelope = ErrorResponse(
            error_code="internal_error",
            message="An internal error occurred.",
            retry_safe=False,
            request_id=request_id,
        )
        headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
        return JSONResponse(
            envelope.model_dump(exclude_none=False),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            headers=headers,
        )


def raise_with_code(
    *,
    status_code: int,
    error_code: ErrorCode,
    message: str,
    retry_after_seconds: int | None = None,
    headers: dict[str, str] | None = None,
    detail: dict[str, Any] | None = None,
) -> NoReturn:
    """Shorthand for callers that want to set an explicit error_code.

    Stashes the error_code + message + retry_after on the
    ``HTTPException.detail`` dict via the sentinel keys the handler
    recognizes — no new exception class needed.

    ``headers`` lets the caller set ``Retry-After`` directly on the
    response (the handler preserves caller-set headers).
    """
    payload: dict[str, Any] = dict(detail or {})
    payload[_DETAIL_CODE_KEY] = error_code
    payload[_DETAIL_MESSAGE_KEY] = message
    if retry_after_seconds is not None:
        payload[_DETAIL_RETRY_AFTER_KEY] = retry_after_seconds
    raise HTTPException(status_code=status_code, detail=payload, headers=headers)


__all__ = [
    "ErrorResponse",
    "install_error_handlers",
    "raise_with_code",
]
