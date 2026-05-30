"""Issue #371: ``GET /search`` for URL-shareable filter state.

The HTMX form posts to ``POST /search`` and the URL never updates;
operators can't share a link that reproduces ``all deprecated docs
from platform from the last 90 days``. The fix adds:

* ``GET /search`` route accepting the same query / filter params
  the POST handler takes. Renders the full ``search.html`` page
  with results pre-populated.
* Status checkboxes become conditional on ``selected_statuses``
  rather than statically ``checked``, so a request like
  ``/search?q=foo&status=deprecated`` arrives with only the
  ``deprecated`` box checked.
* The query input ``value=`` populates from ``?q=``.
* No-JS fallback: the ``<form>`` now carries ``action="/search"
  method="get"`` so a form submit on a browser without HTMX still
  produces a shareable URL.

These integration tests pin the contract end-to-end against a
live pgvector. The companion source-grep tests on ``kiln-app.js``
(``tests/unit/test_url_state_replacestate.py``) pin the
``history.replaceState`` + ``popstate`` JS wiring.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.ingestion.embedding import MockEmbeddingProvider
from cf_knowledge_kiln.ingestion.pipeline import run_source
from cf_knowledge_kiln.ingestion.sources import LocalSource

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings(
        ingest_max_file_bytes=1_048_576,
        ingest_max_files=100,
        ingest_max_repo_bytes=10 * 1_048_576,
    )


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncSession:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """DB-configured TestClient (mirrors :file:`test_web_ui.py`).

    The top-level :file:`tests/conftest.py` fixture doesn't bind
    ``KILN_DATABASE_URL``, so the route's ``Depends(get_session)``
    would return 503 on every call. Bind the test DSN around the
    client lifetime so the GET /search handler hits a real pgvector.
    """
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


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha widgets
            zebra alpha unique-token-aaa widgets.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus: Path) -> None:
    src = LocalSource(name="url-state-tests", type="local", path=str(corpus), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


class TestGetSearchRouteRenders:
    """``GET /search`` returns the full HTML page (not a fragment).
    Form is pre-populated from query-string state."""

    def test_get_search_with_query_renders_full_page(self, client: TestClient) -> None:
        # Full page = base.html shell with <html> / <body> /
        # masthead — distinguishes from the POST fragment.
        response = client.get("/search?q=hello")
        assert response.status_code == 200
        body = response.text
        assert "<html" in body
        assert "<body" in body
        # Query is pre-populated in the input.
        assert 'value="hello"' in body

    def test_get_search_without_query_renders_empty_shell(self, client: TestClient) -> None:
        """Bare ``/search`` (no params) is the same as ``/`` — empty
        results, default form state. Lets users navigate to the
        URL-routed page even when they don't have a query yet."""
        response = client.get("/search")
        assert response.status_code == 200
        body = response.text
        assert "<html" in body
        assert "result-card" not in body  # no results yet

    def test_get_search_returns_html_content_type(self, client: TestClient) -> None:
        response = client.get("/search?q=anything")
        assert response.headers["content-type"].startswith("text/html")


class TestGetSearchPrePopulatesFormFields:
    """Each query-string param must round-trip to the rendered
    form so a pasted URL reproduces the same filter state."""

    def test_query_param_populates_input(self, client: TestClient) -> None:
        response = client.get("/search?q=zebra+alpha")
        # FastAPI's URL decoding converts + to space.
        assert 'value="zebra alpha"' in response.text

    def test_status_param_checks_only_those_statuses(self, client: TestClient) -> None:
        """When ``?status=deprecated`` is the only status param, the
        ``deprecated`` checkbox must be checked AND the default
        ``active`` + ``approved`` checkboxes must NOT be checked
        (the URL is canonical — defaults don't override explicit
        selection)."""
        response = client.get("/search?q=x&status=deprecated")
        body = response.text
        # Find the deprecated status checkbox row.
        assert 'value="deprecated" checked' in body or (
            'value="deprecated"' in body and "checked" in body
        )
        # The active checkbox must NOT carry checked in this request.
        active_idx = body.find('value="active"')
        assert active_idx != -1
        # Slice the input tag opening to its closing > and search.
        end = body.find(">", active_idx)
        active_tag = body[active_idx - 50 : end + 1]
        assert "checked" not in active_tag, (
            "GET /search with explicit ?status=deprecated must not "
            "pre-check 'active' — the URL is canonical."
        )

    def test_repo_param_populates_rail_field(self, client: TestClient) -> None:
        response = client.get("/search?q=x&repo=platform/runbooks")
        assert 'value="platform/runbooks"' in response.text

    def test_owner_param_populates_rail_field(self, client: TestClient) -> None:
        response = client.get("/search?q=x&owner=platform")
        assert 'value="platform"' in response.text

    def test_tags_param_populates_rail_field(self, client: TestClient) -> None:
        response = client.get("/search?q=x&tags=cf,bosh")
        assert 'value="cf,bosh"' in response.text

    def test_last_reviewed_after_populates_date_field(self, client: TestClient) -> None:
        response = client.get("/search?q=x&last_reviewed_after=2026-01-15")
        assert 'value="2026-01-15"' in response.text


class TestGetSearchRunsRetrieval:
    """When the URL carries a query, the page renders WITH results
    (not an empty shell that would require a second client-side
    submit). This is the round-trip the issue's acceptance test
    spells out: copy URL → paste in new tab → same query + filters
    visible."""

    def test_get_search_with_query_runs_retrieval(
        self, client: TestClient, session: AsyncSession, small_corpus: Path
    ) -> None:
        asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
        response = client.get("/search?q=widgets")
        assert response.status_code == 200
        body = response.text
        # The Alpha doc seeded above contains "widgets" — it must
        # appear in the rendered results, not require a second hit.
        assert "result-card" in body
        assert "Alpha" in body

    def test_get_search_with_unselectable_status_returns_no_results(
        self, client: TestClient, session: AsyncSession, small_corpus: Path
    ) -> None:
        """``?status=archived`` against a corpus of active docs
        renders zero results — same short-circuit the POST path
        uses for empty status."""
        asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
        response = client.get("/search?q=widgets&status=archived")
        assert response.status_code == 200
        # No active docs match an archived-only filter.
        assert "result-card" not in response.text


class TestNoJsFormFallback:
    """Browsers without JS get a GET-form submission. The
    ``<form>`` must carry ``action="/search" method="get"`` so the
    default browser behavior produces a shareable URL even without
    HTMX. (HTMX overrides this with hx-post on the client.)"""

    def test_form_has_action_get_fallback(self, client: TestClient) -> None:
        response = client.get("/")
        body = response.text
        # Look for the form's no-JS action/method on the search form.
        assert 'action="/search"' in body, (
            "Search form must carry action='/search' for the no-JS "
            "fallback (HTMX otherwise hijacks via hx-post)."
        )
        assert 'method="get"' in body, (
            "Search form must carry method='get' for the no-JS "
            "fallback so the user lands on a shareable URL."
        )
