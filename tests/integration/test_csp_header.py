"""Integration smoke for the strict-CSP middleware (#144).

End-to-end via the real FastAPI app (``create_app``) — confirms the
middleware is wired in production order and the header lands on the
real routes that humans hit (the search shell, the HTMX swap
fragments, the health probes).

These tests use a live Postgres because the app factory needs a
non-failing DB to mount the routers. We don't need any seeded
documents for this suite, so we only mount the client.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.api.csp import build_policy
from cf_knowledge_kiln.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    saved = os.environ.get("KILN_DATABASE_URL")
    os.environ["KILN_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        if saved is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved
        get_settings.cache_clear()


def test_csp_header_present_on_search_shell(client: TestClient) -> None:
    """The search shell carries the CSP header — this is the page humans see."""
    response = client.get("/")
    assert response.status_code == 200
    header = response.headers.get("Content-Security-Policy")
    assert header is not None, "missing Content-Security-Policy header on GET /"
    # script + style + font directives are the load-bearing ones for
    # the vendored htmx + self-hosted fonts to actually work.
    assert "script-src 'self'" in header
    assert "style-src 'self'" in header
    assert "font-src 'self'" in header


def test_csp_header_matches_canonical_policy(client: TestClient) -> None:
    """The header value matches the canonical directive string exactly."""
    response = client.get("/")
    assert response.headers["Content-Security-Policy"] == build_policy()


def test_csp_header_present_on_static_assets(client: TestClient) -> None:
    """Vendored htmx + the bundled CSS carry the header too.

    A browser fetching a same-origin script doesn't apply the CSP from
    that file's response — it applies the CSP that came with the host
    document. But operators occasionally probe static assets directly,
    and we want a uniform answer.
    """
    css = client.get("/static/kiln.css")
    assert css.status_code == 200
    assert "Content-Security-Policy" in css.headers

    htmx = client.get("/static/vendor/htmx-2.0.4.min.js")
    assert htmx.status_code == 200
    assert "Content-Security-Policy" in htmx.headers


def test_csp_header_present_on_health_probes(client: TestClient) -> None:
    """`/healthz` and `/readyz` are public — they still get the header."""
    for path in ("/healthz", "/readyz"):
        response = client.get(path)
        # /readyz may report 503 in degraded states; CSP should still be present.
        assert "Content-Security-Policy" in response.headers, path


def test_csp_header_present_on_htmx_swap_fragments(client: TestClient) -> None:
    """The HTMX swap response (POST /search) must also carry the header.

    HTMX swaps insert raw HTML into the live DOM. The browser applies
    the host page's CSP to those inserted nodes — but the swap response
    headers are what an automated probe would read, so we assert both
    the empty-query and the no-match cases.
    """
    # Empty query → empty fragment.
    empty = client.post("/search", data={"query": "  "})
    assert empty.status_code == 200
    assert "Content-Security-Policy" in empty.headers

    # Real query with no seeded corpus → still 200, empty-state fragment.
    no_match = client.post(
        "/search",
        data={"query": "completelynonexistentterm", "status": ["active"]},
    )
    assert no_match.status_code == 200
    assert "Content-Security-Policy" in no_match.headers


def test_csp_directive_is_strict_no_unsafe_inline(client: TestClient) -> None:
    """Defense in depth — assert the rolled-out policy is actually strict."""
    response = client.get("/")
    header = response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in header
    assert "unsafe-eval" not in header
    # No allowlist entries for the legacy CDNs the page used to pull from.
    assert "unpkg.com" not in header
    assert "fonts.googleapis.com" not in header
    assert "fonts.gstatic.com" not in header
