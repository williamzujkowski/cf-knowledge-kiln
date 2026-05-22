"""Per-request observability logging (#178).

A lightweight HTTP middleware that emits one log line per request —
method, path, status, duration — so an operator can see request
latency and error rates from the app logs without standing up a
separate metrics stack.

Health and static paths are skipped: CF polls ``/healthz`` constantly
and a load balancer polls ``/readyz``, so logging them would bury the
signal (the actual ``/v1/*`` + ``/search`` traffic) in poll noise.

Plain stdlib ``logging`` — consistent with the rest of the codebase.
The message is key=value formatted so it greps and parses cleanly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

logger = logging.getLogger("cf_knowledge_kiln.request")

# Paths excluded from per-request logging — health probes + static
# assets are high-frequency, low-signal.
_SKIP_PREFIXES = ("/healthz", "/readyz", "/static")


def install_request_logging(app: FastAPI) -> None:
    """Attach the per-request logging middleware to ``app``.

    Called last in ``create_app`` so it is the outermost layer — the
    measured duration then covers the whole request, including time
    spent in the auth and CSP middleware.
    """

    @app.middleware("http")
    async def _log_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path.startswith(_SKIP_PREFIXES):
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            # An unhandled exception became a 500 downstream; record it
            # with the request context so the traceback isn't orphaned.
            logger.exception(
                "request method=%s path=%s status=500 duration_ms=%.1f",
                request.method,
                path,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "request method=%s path=%s status=%d duration_ms=%.1f",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
        return response
