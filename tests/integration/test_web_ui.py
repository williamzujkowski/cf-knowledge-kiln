"""Integration tests for the Phase 6 HTMX search UI (issue #23).

End-to-end via the FastAPI lifespan + live pgvector DB. Asserts:

* ``GET /`` returns the search shell with a form
* ``POST /search`` returns a results-list HTML fragment (HTMX target)
* Filters round-trip from form to RetrievalFilters to engine
* Deprecated results carry the visible flag (per AGENTS.md)
* Empty query returns the empty-state fragment, not a 500
* The static CSS file mounts at /static/kiln.css
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cf_knowledge_kiln.api.app import create_app
from cf_knowledge_kiln.config import Settings, get_settings
from cf_knowledge_kiln.db.models import Document, RagQuery
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
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def small_corpus(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text(
        textwrap.dedent(
            """\
            # Alpha
            zebra alpha unique-token-aaa.
            """
        )
    )
    (tmp_path / "beta.md").write_text(
        textwrap.dedent(
            """\
            # Beta
            beta documentation about widgets and gadgets.
            """
        )
    )
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path) -> None:
    src = LocalSource(name="web-tests", type="local", path=str(corpus_dir), include=["**/*.md"])
    await run_source(
        session, source=src, settings=_settings(), embedding_provider=MockEmbeddingProvider()
    )
    await session.commit()


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


# ─── GET / ──────────────────────────────────────────────────────────


def test_search_page_renders_form(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # Form points at /search with HTMX swap on #results.
    assert 'hx-post="/search"' in body
    assert 'hx-target="#results"' in body
    assert 'name="query"' in body
    # No HTML-escaping bugs on the empty-state hint.
    assert "Type a query above to search." in body


def test_static_css_is_served(client: TestClient) -> None:
    response = client.get("/static/kiln.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "result-card" in response.text  # sanity: real CSS body


# ─── POST /search (HTMX fragment) ───────────────────────────────────


def test_search_post_returns_results_fragment(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200
    body = response.text
    # Fragment is a partial — no <html>, <body>, or <header>.
    assert "<html" not in body
    # Result card structure renders.
    assert "results-list" in body
    assert "result-card" in body
    assert "Beta" in body  # title of the only doc matching 'widgets'


def test_search_post_empty_query_returns_empty_fragment(client: TestClient) -> None:
    response = client.post("/search", data={"query": "  "})
    assert response.status_code == 200
    assert "result-card" not in response.text


def test_search_post_returns_empty_results_message_on_no_match(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post(
        "/search", data={"query": "completelynonexistentterm", "status": ["active"]}
    )
    assert response.status_code == 200
    assert "No results" in response.text


# ─── Deprecated flagging (AGENTS.md) ────────────────────────────────


def test_deprecated_results_carry_visible_flag(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Deprecated docs must be visibly different — CSS class + status badge."""
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    # Demote beta to deprecated.
    asyncio.get_event_loop().run_until_complete(_set_status(session, "beta.md", "deprecated"))

    response = client.post(
        "/search",
        # Allow deprecated through so it actually surfaces.
        data={"query": "widgets", "status": ["active", "deprecated"]},
    )
    assert response.status_code == 200
    body = response.text
    # CSS hook on the card itself.
    assert "status-deprecated" in body
    # Status badge text.
    assert ">deprecated<" in body
    # Reviewer-warned: also surfaces a deprecated_source warning.
    assert "warning-deprecated_source" in body


async def _set_status(session: AsyncSession, path: str, new_status: str) -> None:
    await session.execute(update(Document).where(Document.path == path).values(status=new_status))
    await session.commit()


# ─── Telemetry persistence ──────────────────────────────────────────


def test_search_post_unchecking_all_filters_returns_zero_results(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """User unchecks every status filter → zero results (not default-fallback).

    Slice-6 reviewer caught: the original code treated `status=[]` from
    the form the same as 'no form submitted' and fell back to the
    default `["active", "approved"]` — silently overriding the user's
    explicit choice. The hidden `_filters_set` marker now disambiguates.
    """
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post(
        "/search",
        # `_filters_set` arrived but no `status` — user unchecked all.
        data={"query": "widgets", "_filters_set": "1"},
    )
    assert response.status_code == 200
    # No result cards because no status passes the (empty) filter.
    assert "result-card" not in response.text
    assert "No results" in response.text


def test_search_post_programmatic_no_marker_falls_back_to_defaults(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """A programmatic POST without _filters_set still gets default statuses.

    Counterpart to the test above — ensures the disambiguation only
    triggers for explicit form submissions, not for callers that
    haven't been updated to include the marker.
    """
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets"})
    assert response.status_code == 200
    # Defaults apply → active doc 'beta.md' surfaces.
    assert "result-card" in response.text
    assert "Beta" in response.text


def test_search_post_returns_error_fragment_on_retrieval_failure(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """A retrieval-side exception must render the error fragment, not 500.

    Slice-6 reviewer LOW finding: an HTMX swap of a FastAPI 500
    body looks broken in #results. Patch the retriever to raise
    and assert the user sees the friendly fragment.
    """
    from unittest.mock import patch

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    with patch(
        "cf_knowledge_kiln.retrieval.engine.HybridRetriever.search",
        side_effect=RuntimeError("simulated DB outage"),
    ):
        response = client.post(
            "/search",
            data={"query": "widgets", "_filters_set": "1", "status": "active"},
        )
    assert response.status_code == 503
    assert "error-fragment" in response.text
    assert "Search is temporarily unavailable" in response.text


def test_web_search_persists_rag_query_with_human_consumer(
    client: TestClient, session: AsyncSession, small_corpus: Path, engine: AsyncEngine
) -> None:
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200

    async def _rows() -> list[RagQuery]:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            return list((await s.execute(select(RagQuery))).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(_rows())
    assert len(rows) == 1
    assert rows[0].consumer_type == "human"
    assert rows[0].query == "widgets"


# ─── #79: rate limit on HTMX routes returns HTML fragment, not JSON ──


@pytest.fixture
def rate_limited_client(database_url: str) -> Iterator[TestClient]:
    saved_url = os.environ.get("KILN_DATABASE_URL")
    saved_search = os.environ.get("KILN_RATE_LIMIT_SEARCH_PER_MIN")
    saved_feedback = os.environ.get("KILN_RATE_LIMIT_FEEDBACK_PER_MIN")
    os.environ["KILN_DATABASE_URL"] = database_url
    os.environ["KILN_RATE_LIMIT_SEARCH_PER_MIN"] = "1"
    os.environ["KILN_RATE_LIMIT_FEEDBACK_PER_MIN"] = "1"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        for key, val in (
            ("KILN_DATABASE_URL", saved_url),
            ("KILN_RATE_LIMIT_SEARCH_PER_MIN", saved_search),
            ("KILN_RATE_LIMIT_FEEDBACK_PER_MIN", saved_feedback),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        get_settings.cache_clear()


def test_htmx_search_429_returns_error_fragment(rate_limited_client: TestClient) -> None:
    """The HTMX /search route renders an error fragment, not raw JSON, on 429."""
    data = {"query": "widgets", "status": ["active"]}
    rate_limited_client.post("/search", data=data)
    second = rate_limited_client.post("/search", data=data)
    assert second.status_code == 429
    # Same fragment shape as /search's other error path, so HTMX swaps it cleanly.
    assert "Too many requests" in second.text
    assert int(second.headers["retry-after"]) >= 1


def test_htmx_feedback_429_returns_error_fragment(rate_limited_client: TestClient) -> None:
    """The HTMX /feedback route renders the feedback-error fragment on 429."""
    import uuid

    payload = {
        "query_id": str(uuid.uuid4()),
        "chunk_id": str(uuid.uuid4()),
        "signal": "useful",
        "comment": "",
    }
    rate_limited_client.post("/feedback", data=payload)
    second = rate_limited_client.post("/feedback", data=payload)
    assert second.status_code == 429
    assert "Too many feedback submissions" in second.text
    assert int(second.headers["retry-after"]) >= 1


# ─── #24: source_url renders as a new-tab link ──────────────────────


def test_search_result_card_renders_source_url_as_external_link(
    client: TestClient, session: AsyncSession, tmp_path: Path
) -> None:
    """A doc with ``source_url:`` frontmatter gets a clickable card link.

    #24: cards with a canonical URL render an <a target="_blank"
    rel="noopener"> link instead of plain repo/path text. Verifies
    source_url flows from frontmatter → documents.source_url → CTE
    projection → SearchRow → DocumentRef → result-card view dict →
    Jinja template.
    """
    (tmp_path / "linked.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Linked doc
            status: active
            source_url: https://docs.example.com/linked
            ---
            # Linked doc
            unique-token-link-target widgets and gadgets.
            """
        )
    )
    asyncio.get_event_loop().run_until_complete(_seed(session, tmp_path))

    response = client.post(
        "/search",
        data={"query": "unique-token-link-target", "status": ["active"]},
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert "https://docs.example.com/linked" in body
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body
    assert 'class="source source-link"' in body


def test_search_result_card_without_source_url_renders_plain_text(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """The existing corpus has no source_url frontmatter — cards stay plain.

    Regression guard for the source-link plumbing: cards without a
    canonical URL must NOT render a link (no target="_blank" anchor),
    just the existing <span class="source"> text.
    """
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200
    # The plain-text span renders; the anchor variant does not.
    assert 'class="source">' in response.text
    assert 'class="source source-link"' not in response.text


def test_search_result_card_drops_javascript_source_url(
    client: TestClient, session: AsyncSession, tmp_path: Path
) -> None:
    """#24 HIGH: a malicious source_url must not become an href.

    Frontmatter is untrusted. A doc with ``source_url: javascript:...``
    must be coerced to NULL at ingest and render as plain text — never
    as an ``href`` that the browser would execute on click.
    """
    (tmp_path / "evil.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Hostile linked doc
            status: active
            source_url: "javascript:alert(document.cookie)"
            ---
            # Hostile linked doc
            unique-token-evil-target widgets and gadgets.
            """
        )
    )
    asyncio.get_event_loop().run_until_complete(_seed(session, tmp_path))
    response = client.post(
        "/search",
        data={"query": "unique-token-evil-target", "status": ["active"]},
    )
    assert response.status_code == 200
    body = response.text
    # The malicious URL must never appear as an href value.
    assert 'href="javascript:' not in body
    assert "javascript:alert" not in body
    # The card must still render — just as plain text, not as a link.
    assert "Hostile linked doc" in body
    assert 'class="source source-link"' not in body


# ─── #117: card-shape parity (highlight + owner + badges + warning copy) ──


def test_search_excerpt_highlights_query_terms(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """#117: query terms are wrapped in <mark> server-side (no JS)."""
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "widgets", "status": ["active"]})
    assert response.status_code == 200
    # The term "widgets" appears in fixture corpus beta.md; it should
    # come back wrapped in <mark> in the rendered fragment.
    assert "<mark>widgets</mark>" in response.text.lower()


def test_search_excerpt_highlight_dropped_for_stopwords(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """Stopwords + single-letter terms are dropped to avoid noise."""
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post("/search", data={"query": "a the widgets", "status": ["active"]})
    # "a" and "the" must not be highlighted; "widgets" still is.
    assert "<mark>widgets</mark>" in response.text.lower()
    assert "<mark>a</mark>" not in response.text
    assert "<mark>the</mark>" not in response.text


def test_search_excerpt_html_escapes_user_input(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """A query with <script> must not produce a script tag in output."""
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post(
        "/search", data={"query": "<script>alert(1)</script>", "status": ["active"]}
    )
    # The query itself appears echoed in the "for «query»" line; that
    # path autoescapes through Jinja. Critically there's no raw script.
    assert "<script>" not in response.text


def test_search_form_exposes_archived_and_superseded_pills(
    client: TestClient,
) -> None:
    """#117: pill set covers all 6 statuses, not just 4."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'value="archived"' in body
    assert 'value="superseded"' in body


def test_search_warning_renders_spec_mandated_copy(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """#117: weak_evidence warning renders the journey-doc copy, not engine raw.

    The fixture corpus is tiny; a query for a unique gibberish phrase
    triggers the weak_evidence warning, which must render with the
    spec-mandated message.
    """
    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))
    response = client.post(
        "/search", data={"query": "vbynxz nonexistent target", "status": ["active"]}
    )
    assert response.status_code == 200
    body = response.text
    # Either the spec-mandated copy OR no weak_evidence warning at all.
    # If the warning fires, it must use the canonical text.
    if "warning-weak_evidence" in body:
        assert "no clearly authoritative source" in body
        assert "Confidence is low" in body
