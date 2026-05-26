"""Unit tests for the stable ``untrusted_content_notice_id`` field.

Agent-API audit MEDIUM finding (post-#292 / #299): the
``untrusted_content_notice`` field on ``ContextPackResponse`` /
``AnswerResponse`` is a free-form prose string. Agents that want
to localize the preamble or detect "the server changed the notice
between releases" have to byte-compare the prose.

This PR adds a sibling ``untrusted_content_notice_id`` —
semver-versioned, stable across non-breaking edits to the prose,
bumped when the meaning changes. Agents switch on the id; the
prose is the human-readable rendering.

Contract:

* The id is shaped ``kiln.untrusted-content.vN`` with N as the
  major version.
* Bumping the prose text without changing the meaning does NOT
  change the id.
* Adding a new constraint (e.g., "and you must cite the chunk_id
  when you use the content") DOES change the id.
* Agents that don't recognize the id should fall back to refusing
  to treat source text as instructions (the safe default).
"""

from __future__ import annotations

import re
from uuid import uuid4

from cf_knowledge_kiln.agent.serializers import (
    UNTRUSTED_CONTENT_NOTICE,
    UNTRUSTED_CONTENT_NOTICE_ID,
)


class TestUntrustedContentNoticeId:
    """Pin the (prose, id) contract."""

    def test_id_is_defined(self) -> None:
        """The constant exists and is a non-empty string. Importing
        it from the serializer module is what callers will do; pin
        the public surface."""
        assert UNTRUSTED_CONTENT_NOTICE_ID
        assert isinstance(UNTRUSTED_CONTENT_NOTICE_ID, str)

    def test_id_follows_versioned_shape(self) -> None:
        """The id MUST be ``kiln.untrusted-content.vN`` where N is
        a positive integer. Codegen clients and grep-based audits
        can rely on this shape to detect drift across releases."""
        assert re.fullmatch(r"kiln\.untrusted-content\.v\d+", UNTRUSTED_CONTENT_NOTICE_ID), (
            f"id must match kiln.untrusted-content.vN; got {UNTRUSTED_CONTENT_NOTICE_ID!r}"
        )

    def test_prose_constant_unchanged(self) -> None:
        """The prose preamble itself is unchanged by this PR — the
        id is additive. A regression that drops or restates the
        prose would have to bump the id; pin both together."""
        # Substring check tolerant to future minor copy edits, but
        # the load-bearing 'source evidence' phrase MUST remain.
        assert "source evidence" in UNTRUSTED_CONTENT_NOTICE.lower()
        assert "do not treat" in UNTRUSTED_CONTENT_NOTICE.lower()


class TestContextPackResponseCarriesNoticeId:
    """The ContextPackResponse model exposes the id alongside the
    prose so agents can switch on the stable handle."""

    def test_response_model_has_notice_id_field(self) -> None:
        from cf_knowledge_kiln.retrieval.types import ContextPackResponse

        fields = ContextPackResponse.model_fields
        assert "untrusted_content_notice_id" in fields, (
            "ContextPackResponse must expose untrusted_content_notice_id"
        )

    def test_field_is_required_on_construction(self) -> None:
        """The field is required (not Optional, no default). A
        constructor call missing it raises Pydantic ValidationError.
        Without this contract the field could silently default to
        None and agents that rely on the id would see None."""
        import pytest
        from pydantic import ValidationError

        from cf_knowledge_kiln.retrieval.types import (
            ContextPackResponse,
            TokenBudget,
        )

        with pytest.raises(ValidationError) as exc_info:
            ContextPackResponse(
                context_pack_id=uuid4(),
                answerable=False,
                evidence=[],
                warnings=[],
                token_budget=TokenBudget(requested=1, used_estimate=0),
                requires_human_review=False,
                untrusted_content_notice="prose",
                # untrusted_content_notice_id deliberately omitted
            )
        errors = exc_info.value.errors()
        assert any(e.get("loc") == ("untrusted_content_notice_id",) for e in errors)

    def test_field_round_trips_through_construction(self) -> None:
        """Belt-and-braces: the id value the caller passes in is the
        id value the serialized response carries out. No silent
        default-substitution from the model."""
        from cf_knowledge_kiln.retrieval.types import (
            ContextPackResponse,
            TokenBudget,
        )

        pack = ContextPackResponse(
            context_pack_id=uuid4(),
            answerable=False,
            evidence=[],
            warnings=[],
            token_budget=TokenBudget(requested=1, used_estimate=0),
            requires_human_review=False,
            untrusted_content_notice="prose",
            untrusted_content_notice_id=UNTRUSTED_CONTENT_NOTICE_ID,
        )
        assert pack.untrusted_content_notice_id == UNTRUSTED_CONTENT_NOTICE_ID
        # And it round-trips through JSON serialization.
        dumped = pack.model_dump(mode="json")
        assert dumped["untrusted_content_notice_id"] == UNTRUSTED_CONTENT_NOTICE_ID


class TestAnswerResponseCarriesNoticeId:
    """The AnswerResponse model exposes the id on every code path
    (synthesized, refused, no-evidence, generator-down)."""

    def test_response_model_has_notice_id_field(self) -> None:
        from cf_knowledge_kiln.retrieval.types import AnswerResponse

        fields = AnswerResponse.model_fields
        assert "untrusted_content_notice_id" in fields
