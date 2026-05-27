"""Pins the #337 fix: result-card surfaces chunk_index inline.

Audit Finding #2: the preview shows "Selected chunk #07" but the
result card had no chunk-index marker — a user couldn't tell
"matched section 2 of …" vs "matched section 7 of …" at scan
speed. Now: `chunk_index` flows from SearchRow → RankedChunk →
ResultCard, the template renders "section N" (1-based) inline
on the source-line, and the field is optional on the OpenAPI
wire (additive per ADR-0011).

The complementary "of M" piece (total chunks per document) is
issue #384 — requires a second query or window function in the
hybrid SELECT.
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


class TestResultCardPydanticShape:
    """The Pydantic ResultCard carries an optional chunk_index field."""

    def test_chunk_index_optional(self) -> None:
        from uuid import uuid4

        from cf_knowledge_kiln.retrieval.types import ResultCard

        # Old fixture style: no chunk_index passed → field is None.
        c = ResultCard(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="t",
            excerpt="x",
            status="active",
            score=0.5,
        )
        assert c.chunk_index is None

    def test_chunk_index_must_be_non_negative(self) -> None:
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
                chunk_index=-1,
            )

    def test_chunk_index_zero_is_legal(self) -> None:
        """The first chunk in a doc is index 0; the template renders
        it as 'section 1' (1-based for display)."""
        from uuid import uuid4

        from cf_knowledge_kiln.retrieval.types import ResultCard

        c = ResultCard(
            chunk_id=uuid4(),
            document_id=uuid4(),
            title="t",
            excerpt="x",
            status="active",
            score=0.5,
            chunk_index=0,
        )
        assert c.chunk_index == 0


class TestTemplateRendersChunkIndex:
    """Source-grep the template directly — full-render needs the
    affordance fixture cascade which other tests already cover."""

    def test_template_carries_one_based_render(self) -> None:
        text = (_TEMPLATES / "_results.html").read_text()
        # The 1-based display arithmetic (+ 1 on the index) is the
        # contract for the user-facing "section N" copy. Pin so a
        # future refactor doesn't silently switch to 0-based and
        # confuse users who count from 1.
        assert "{{ r.chunk_index + 1 }}" in text

    def test_template_block_is_guarded_for_legacy_fixtures(self) -> None:
        """The Jinja guard MUST defend against fixtures that don't
        supply chunk_index (existing affordance + status-badge tests
        pass dicts without the key). Use `is defined and is not
        none` rather than bare `is not none` (which raises
        UndefinedError on missing keys)."""
        text = (_TEMPLATES / "_results.html").read_text()
        # Anchor on the comment + the specific guard shape.
        assert "r.chunk_index is defined and r.chunk_index is not none" in text

    def test_chunk_index_span_is_aria_hidden(self) -> None:
        """The chunk-index is a sighted-only convenience; the
        wrapper's aria-label already announces the source. Avoid
        making AT users hear an extra 'section 7' on every card."""
        text = (_TEMPLATES / "_results.html").read_text()
        idx = text.index('class="chunk-index"')
        # Window covers the opening tag.
        open_lt = text.rfind("<", 0, idx)
        close_gt = text.find(">", idx)
        tag = text[open_lt : close_gt + 1]
        assert 'aria-hidden="true"' in tag


class TestSearchRowCarriesChunkIndex:
    """The DB layer joins document_chunks.chunk_index into SearchRow
    so the engine doesn't re-query."""

    def test_search_row_dataclass_has_chunk_index(self) -> None:
        from dataclasses import fields

        from cf_knowledge_kiln.db.repositories._hybrid import SearchRow

        names = {f.name for f in fields(SearchRow)}
        assert "chunk_index" in names

    def test_ranked_chunk_carries_chunk_index(self) -> None:
        from dataclasses import fields

        from cf_knowledge_kiln.retrieval.ranking import RankedChunk

        names = {f.name for f in fields(RankedChunk)}
        assert "chunk_index" in names

    def test_boost_pass_preserves_chunk_index(self) -> None:
        """apply_boosts re-constructs RankedChunk; chunk_index must
        survive (otherwise the boosted set on the result side
        always reports section 0)."""
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
        )
        [boosted] = apply_boosts([chunk], config=RetrievalConfig(), today=date.today())
        assert boosted.chunk_index == 7


class TestOpenAPISchemaPinsField:
    def test_result_card_schema_carries_chunk_index(self) -> None:
        from pathlib import Path

        yaml = (Path(__file__).resolve().parents[2] / "openapi/openapi.yaml").read_text()
        idx = yaml.index("ResultCard:")
        block = yaml[idx : idx + 3500]
        assert "chunk_index" in block
        # Optional + minimum: 0 + nullable.
        assert "minimum: 0" in block
        assert "nullable: true" in block
