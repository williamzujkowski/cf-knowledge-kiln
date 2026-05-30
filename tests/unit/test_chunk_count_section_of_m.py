"""Pins the #384 fix: result-card renders "section N of M".

The "of M" half of the #337 audit finding. #385 shipped
``chunk_index`` (the "section N" part); this test suite pins the
total chunk count per document so the UI reads "section 7 of 32"
rather than just "section 7".

The two fields flow together through the same hybrid SQL row
(``chunk_count`` is a correlated subquery on ``document_chunks``
keyed by ``document_id``), but they remain independent on the
wire — a synthetic ResultCard with only ``chunk_index`` set still
renders cleanly as bare "section N".
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "src/cf_knowledge_kiln/api/templates"


@pytest.fixture
def env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
    )
    env.globals["url_for"] = lambda *_a, **_kw: "/static/stub.css"
    env.globals["agent_guide_url"] = lambda: None
    return env


# ─── Pydantic shape ──────────────────────────────────────────────────


class TestResultCardCarriesChunkCount:
    def test_chunk_count_optional(self) -> None:
        from uuid import uuid4

        from cf_knowledge_kiln.retrieval.types import ResultCard

        # Old-style fixture: no chunk_count → None (additive per ADR-0011).
        c = ResultCard(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="t",
            excerpt="x",
            status="active",
            score=0.5,
        )
        assert c.chunk_count is None

    def test_chunk_count_must_be_non_negative(self) -> None:
        from uuid import uuid4

        from pydantic import ValidationError

        from cf_knowledge_kiln.retrieval.types import ResultCard

        with pytest.raises(ValidationError):
            ResultCard(
                chunk_id=uuid4(),
                document_id=uuid4(),
                title="t",
                excerpt="x",
                status="active",
                score=0.5,
                chunk_count=-1,
            )

    def test_chunk_count_zero_is_legal(self) -> None:
        """Documents with zero chunks shouldn't appear in retrieval
        (they have no embedding rows to match), but the field still
        admits zero so a defensive caller doesn't have to special-
        case it. The template treats 0 as 'unknown' and skips the
        ' of 0' rendering — that's a UI concern, not a model one."""
        from uuid import uuid4

        from cf_knowledge_kiln.retrieval.types import ResultCard

        c = ResultCard(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="t",
            excerpt="x",
            status="active",
            score=0.5,
            chunk_count=0,
        )
        assert c.chunk_count == 0


# ─── Template renders "section N of M" ───────────────────────────────


def _render(env: jinja2.Environment, result: dict[str, object]) -> str:
    """Render a single result card through ``_results.html``."""
    base: dict[str, object] = {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "title": "Example",
        "excerpt_html": "x",
        "excerpt_full_html": "x",
        "heading_path": [],
        "heading_path_str": "",
        "repo": "owner/repo",
        "path": "doc.md",
        "source_url": None,
        "owner": None,
        "status": "active",
        "last_reviewed": None,
        "score": 0.5,
        "score_tier": 3,
        "deprecation_label": None,
        "status_tooltip": None,
        "warnings": [],
    }
    base.update(result)
    return env.get_template("_results.html").render(
        query="x",
        results=[base],
        warnings=[],
        query_id=None,
        filters={},
        selected_statuses=["active"],
    )


class TestTemplateRendersSectionOfM:
    def test_renders_both_index_and_count(self, env: jinja2.Environment) -> None:
        body = _render(env, {"chunk_index": 6, "chunk_count": 32})
        # chunk_index is 0-based + 1 = 7; chunk_count is total = 32.
        assert "section 7 of 32" in body

    def test_falls_back_to_bare_section_n_without_count(self, env: jinja2.Environment) -> None:
        """The chunk_count field is independent on the wire; a
        result with only chunk_index set MUST render as bare
        'section N' rather than 'section N of None' or omitting the
        line entirely. This is the contract for synthetic fixtures
        and for any future caller that doesn't query the count."""
        body = _render(env, {"chunk_index": 6})
        assert "section 7" in body
        # No "of M" should appear.
        assert " of " not in body.split("section 7", 1)[1].split("</em>", 1)[0]

    def test_count_zero_treated_as_unknown(self, env: jinja2.Environment) -> None:
        """The template falls back to bare 'section N' when count is
        0. Synthetic RankedChunks default chunk_count to 0; surfacing
        ' of 0' would be a worse UX than omitting the suffix."""
        body = _render(env, {"chunk_index": 6, "chunk_count": 0})
        assert "section 7" in body
        # The 'of 0' suffix MUST NOT appear.
        section = body.split("section 7", 1)[1].split("</em>", 1)[0]
        assert " of 0" not in section

    def test_count_only_does_not_render(self, env: jinja2.Environment) -> None:
        """The whole chunk-index span hides when chunk_index is
        absent — even if a defensive caller set chunk_count without
        it. The index drives the 'section N' phrase; without it the
        line is meaningless."""
        body = _render(env, {"chunk_count": 32})
        assert "chunk-index" not in body

    def test_template_count_guard_is_defensive(self) -> None:
        """The template MUST use ``is defined and r.chunk_count``
        (not bare ``r.chunk_count``) so dict fixtures from #385-era
        tests (no chunk_count key) keep rendering as bare
        'section N'."""
        text = (_TEMPLATES / "_results.html").read_text()
        assert "r.chunk_count is defined and r.chunk_count" in text


# ─── Dataclass + boost-preservation contracts ────────────────────────


class TestSearchRowAndRankedChunkCarryChunkCount:
    def test_search_row_dataclass_has_chunk_count(self) -> None:
        from dataclasses import fields

        from cf_knowledge_kiln.db.repositories._hybrid import SearchRow

        names = {f.name for f in fields(SearchRow)}
        assert "chunk_count" in names

    def test_ranked_chunk_carries_chunk_count(self) -> None:
        from dataclasses import fields

        from cf_knowledge_kiln.retrieval.ranking import RankedChunk

        names = {f.name for f in fields(RankedChunk)}
        assert "chunk_count" in names

    def test_boost_pass_preserves_chunk_count(self) -> None:
        """apply_boosts re-constructs RankedChunk; chunk_count must
        survive (otherwise boosted cards always report 'of 0' and
        the template hides the suffix)."""
        from datetime import date
        from uuid import uuid4

        from cf_knowledge_kiln.retrieval.config import RetrievalConfig
        from cf_knowledge_kiln.retrieval.ranking import RankedChunk, apply_boosts

        chunk = RankedChunk(
            chunk_id=uuid4(),
            document_id=uuid4(),
            score=0.9,
            status="active",
            heading_path=(),
            last_reviewed=date.today(),
            chunk_index=7,
            chunk_count=32,
        )
        [boosted] = apply_boosts([chunk], config=RetrievalConfig(), today=date.today())
        assert boosted.chunk_count == 32


# ─── OpenAPI schema pin ──────────────────────────────────────────────


class TestOpenAPISchemaPinsChunkCount:
    def test_result_card_schema_carries_chunk_count(self) -> None:
        from pathlib import Path

        yaml = (Path(__file__).resolve().parents[2] / "openapi/openapi.yaml").read_text()
        idx = yaml.index("ResultCard:")
        block = yaml[idx : idx + 4000]
        assert "chunk_count:" in block, "chunk_count missing from ResultCard schema"
        # The minimum + nullable constraints mirror chunk_index.
        # Find the chunk_count entry and check the immediate properties.
        ci = block.index("chunk_count:")
        entry = block[ci : ci + 400]
        assert "minimum: 0" in entry
        assert "nullable: true" in entry


# ─── Web view-shaper threads both fields ────────────────────────────


class TestResultCardViewSurfacesBothFields:
    """The HTMX template gets a dict from ``_result_card_view``, not
    the Pydantic ResultCard. Both ``chunk_index`` and ``chunk_count``
    MUST appear in the dict — otherwise the "section N of M" line
    renders blank in the HTMX flow even though the JSON ResultCard
    carries the data correctly."""

    def test_view_includes_chunk_index_and_count(self) -> None:
        from dataclasses import dataclass
        from datetime import date
        from uuid import uuid4

        from cf_knowledge_kiln.api.result_cards import result_card_view as _result_card_view

        @dataclass
        class _Chunk:
            chunk_id: object
            document_id: object
            score: float = 0.5
            status: str = "active"
            heading_path: tuple = ()
            last_reviewed: date | None = None
            chunk_index: int = 6
            chunk_count: int = 32
            authority: str | None = None

        @dataclass
        class _Ref:
            title: str = "Example"
            repo: str = "owner/repo"
            path: str = "doc.md"
            source_url: str | None = None
            commit_sha: str | None = None
            owner: str | None = None
            authority: str | None = None

        view = _result_card_view(
            _Chunk(chunk_id=uuid4(), document_id=uuid4()),
            _Ref(),
            "body content",
            query="",
        )
        assert view["chunk_index"] == 6
        assert view["chunk_count"] == 32

    def test_view_count_zero_becomes_none(self) -> None:
        """Synthetic chunks default chunk_count to 0; the view
        layer surfaces 0 as None so the template renders bare
        'section N' rather than 'section N of 0'."""
        from dataclasses import dataclass
        from datetime import date
        from uuid import uuid4

        from cf_knowledge_kiln.api.result_cards import result_card_view as _result_card_view

        @dataclass
        class _Chunk:
            chunk_id: object
            document_id: object
            score: float = 0.5
            status: str = "active"
            heading_path: tuple = ()
            last_reviewed: date | None = None
            chunk_index: int = 0
            chunk_count: int = 0
            authority: str | None = None

        @dataclass
        class _Ref:
            title: str = "Example"
            repo: str = "owner/repo"
            path: str = "doc.md"
            source_url: str | None = None
            commit_sha: str | None = None
            owner: str | None = None
            authority: str | None = None

        view = _result_card_view(
            _Chunk(chunk_id=uuid4(), document_id=uuid4()),
            _Ref(),
            "body content",
            query="",
        )
        assert view["chunk_count"] is None
