"""Integration tests for the preview-panel HTMX loading skeleton (#287).

Two pieces:

* Title button must carry ``hx-indicator="#preview"`` so HTMX puts
  ``.htmx-request`` on the panel during the fetch. Without this,
  no class change happens on the target and the skeleton overlay
  rule never matches.
* CSS rule ``#preview.htmx-request::before`` must exist in the
  regenerated kiln.css bundle. Without it, the class change is a
  no-op and the user still sees no loading signal.

Pattern matches the other CSS bundle assertions in
:mod:`tests.integration.test_results_mobile_css`.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import AsyncIterator, Iterator
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
    return tmp_path


async def _seed(session: AsyncSession, corpus_dir: Path) -> None:
    src = LocalSource(
        name="skeleton-tests", type="local", path=str(corpus_dir), include=["**/*.md"]
    )
    await run_source(
        session,
        source=src,
        settings=_settings(),
        embedding_provider=MockEmbeddingProvider(),
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


def test_title_button_carries_hx_indicator_for_preview(
    client: TestClient, session: AsyncSession, small_corpus: Path
) -> None:
    """The title-button POSTs hx-get against /preview/{id} → #preview.
    The new hx-indicator='#preview' tells HTMX to add .htmx-request
    to the target during the fetch. Without it, no class change
    happens on the panel and the skeleton overlay rule never
    matches. Pins the wiring at the rendered-template level."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(_seed(session, small_corpus))

    response = client.post("/search", data={"query": "alpha", "status": ["active"]})
    assert response.status_code == 200
    body = response.text
    # The result-title button must carry the hx-indicator attribute.
    # Match the attribute regardless of position relative to other
    # hx-* attrs on the same element.
    assert 'hx-indicator="#preview"' in body


def test_kiln_css_has_preview_skeleton_rule(client: TestClient) -> None:
    """The CSS rule that shows the skeleton overlay must be present
    in the regenerated bundle. Without it, the .htmx-request class
    is a no-op on the panel and the user sees no loading signal."""
    body = client.get("/static/kiln.css").text
    # The rule MUST be scoped to #preview.htmx-request — not
    # generic .htmx-request — so it only fires for preview loads,
    # not the global /search fetch (which uses .htmx-bar).
    assert "#preview.htmx-request" in body
    # And it draws the skeleton via a pseudo-element (::before or
    # ::after) so the previous content stays in the DOM underneath.
    assert "#preview.htmx-request::before" in body


def test_skeleton_uses_wcag_visible_fill(client: TestClient) -> None:
    """Reviewer-flagged: --paper-dim against --paper is 1.10:1, well
    below WCAG 1.4.11's 3:1 non-text contrast floor. The shimmer
    bars MUST use --rule-strong (~3:1) instead so users with low
    vision can perceive the loading state."""
    body = client.get("/static/kiln.css").text
    # The three bar fills are --rule-strong, not --paper-dim.
    # Scope the search to the skeleton rule body so unrelated
    # uses of either token elsewhere don't false-positive.
    import re

    m = re.search(r"#preview\.htmx-request::before\s*\{([^}]*)\}", body, re.DOTALL)
    assert m is not None, "skeleton ::before rule missing from bundle"
    rule_body = m.group(1)
    assert "var(--rule-strong)" in rule_body
    assert "var(--paper-dim)" not in rule_body


def test_preview_close_stays_above_skeleton_overlay(client: TestClient) -> None:
    """Reviewer-flagged: the ::before overlay sits at z-index 1.
    The close button has no positioned ancestor of its own and
    would render BENEATH the overlay during loading, hiding its
    focus ring. The button MUST carry an explicit z-index above
    the overlay so AT users tabbing into it can see focus."""
    body = client.get("/static/kiln.css").text
    import re

    # Locate the .preview-close rule body (the base rule, not :hover)
    # and confirm it sets z-index above 1.
    m = re.search(r"\.preview-close\s*\{([^}]*)\}", body, re.DOTALL)
    assert m is not None
    rule_body = m.group(1)
    assert "z-index" in rule_body
    # And position is set so z-index has effect (auto position
    # ignores z-index entirely).
    assert "position:" in rule_body


def test_skeleton_does_not_clobber_panel_position(client: TestClient) -> None:
    """Reviewer-flagged: an earlier draft had
    `.preview-panel.htmx-request { position: relative }` which
    overrode the breakpoint sticky/fixed positions and shifted
    the panel between coordinate spaces during loading. Pin the
    fix: the htmx-request rule MUST NOT carry a position
    declaration of its own."""
    body = client.get("/static/kiln.css").text
    import re

    m = re.search(r"\.preview-panel\.htmx-request\s*\{([^}]*)\}", body, re.DOTALL)
    # Either the rule is absent entirely (preferred) or it has no
    # position declaration. Both are acceptable.
    if m is not None:
        assert "position:" not in m.group(1), (
            "preview-panel.htmx-request must NOT set position — would "
            "clobber breakpoint sticky/fixed positions during loading"
        )
