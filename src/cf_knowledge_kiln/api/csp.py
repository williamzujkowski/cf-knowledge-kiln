"""Strict Content-Security-Policy middleware (#144).

Adds a ``Content-Security-Policy`` header to every HTTP response so the
browser refuses to execute inline scripts, inline styles, or content
fetched from any origin other than ``self``. With #131 (inline JS
moved into ``/static/kiln-app.js``) and the #144 changes (htmx + fonts
vendored under ``/static/vendor/``, ``data-i`` replacing the inline
``style="--i: N"``), the page is fully compatible with this policy —
nothing on the wire needs an allowlist entry.

The policy is shipped as a tuple of directive strings joined with
``"; "`` so a reviewer can scan the list line-by-line. Adding or
loosening a directive is a deliberate edit, not a string-mangling
exercise.

Operator opt-outs (env, prefix ``KILN_``):

* ``KILN_CSP_REPORT_ONLY=1`` — emit
  ``Content-Security-Policy-Report-Only`` instead of the enforcing
  header. Useful during a dev rollout to confirm a candidate policy
  doesn't break anything before flipping enforcement on. Default is
  enforcement.

Per AGENTS.md: no secrets, no hard-coded routes, no model-mediated
authorization. The CSP lives in middleware, not in templates.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

# Directive list. Joined with "; " into the final header value.
# Keep one directive per tuple element so a future tweak shows up as a
# one-line diff.
#
# Notes per directive:
#   default-src 'self'   — every fetch falls back to same-origin.
#   script-src 'self'    — no inline <script>, no eval, no unpkg.
#                          (#131 + #144 prerequisite work)
#   style-src 'self'     — no inline `style=`, no Google Fonts CSS.
#                          (#144 dropped the last inline style attr)
#   font-src 'self'      — fonts come from /static/vendor/, not gstatic.
#   img-src 'self' data: — `data:` URIs are widely used for inline SVG
#                          glyphs / favicons; no remote images today.
#   connect-src 'self'   — XHR / fetch / EventSource targets only us.
#   frame-ancestors 'none' — no embedding; this is a search UI, not a
#                          widget. Equivalent to X-Frame-Options: DENY.
#   base-uri 'self'      — block <base> hijacking that would redirect
#                          relative URLs to attacker-controlled origins.
#   form-action 'self'   — forms POST back to us, never to a third party.
#   object-src 'none'    — refuse <object>/<embed>/<applet>; legacy
#                          plugin surfaces have a long history of XSS.
_DIRECTIVES: tuple[str, ...] = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
)

# The enforcing header. The report-only variant uses a different name
# so a browser can pick exactly one of the two. (Sending both is
# legal — they apply independently — but we don't need it here.)
_HEADER_ENFORCE: str = "Content-Security-Policy"
_HEADER_REPORT_ONLY: str = "Content-Security-Policy-Report-Only"


def build_policy() -> str:
    """Return the canonical CSP directive string.

    Public so tests can assert exact directive content without
    constructing the middleware. Joined with ``"; "`` to match the
    spec's serialization grammar.
    """
    return "; ".join(_DIRECTIVES)


def _is_report_only() -> bool:
    """Resolve the report-only flag from the environment.

    Accept the same truthy values pydantic-settings accepts so an
    operator who set ``KILN_CSP_REPORT_ONLY=true`` (rather than 1)
    isn't silently ignored. Empty / missing / anything else is False.
    """
    raw = os.environ.get("KILN_CSP_REPORT_ONLY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def install_csp_middleware(app: FastAPI) -> None:
    """Attach the CSP-emitting middleware to ``app``.

    Should be called after auth + rate-limit middleware are installed so
    the header is added on every response — including the 401 / 429
    bodies, which a browser will still parse if the user navigated
    directly to a protected route.
    """
    policy = build_policy()
    header_name = _HEADER_REPORT_ONLY if _is_report_only() else _HEADER_ENFORCE

    @app.middleware("http")
    async def _csp(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        # Never overwrite a header an inner handler explicitly set —
        # leaves a future per-route override path open without further
        # middleware changes. setdefault on a starlette MutableHeaders.
        response.headers.setdefault(header_name, policy)
        return response


__all__ = ["build_policy", "install_csp_middleware"]
