"""Pins the #361 embed_warnings_in_text behavior.

Opt-in flag on ContextPackRequest. When true, the serializer
suffixes each kept chunk's text with one
``[KILN-WARN: <type> — <message>]`` line per non-blocking warning
whose source_id matches the chunk's document_id.

Pins:
* default is False (additive on /v1/)
* marker shape is greppable + namespaced
* blocking-severity warnings are NOT embedded (security)
* global warnings (no source_id) are NOT embedded — they stay in
  the top-level warnings[] array
* idempotency: assembling twice with the same inputs produces
  byte-identical evidence text
* sort order deterministic (by (type, message))
"""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID, uuid4

from cf_knowledge_kiln.agent.serializers import (
    DocumentRef,
    SerializerInputs,
    assemble_context_pack,
)
from cf_knowledge_kiln.retrieval.ranking import RankedChunk
from cf_knowledge_kiln.retrieval.types import ContextPackRequest, Warning


def _build_inputs(
    *,
    chunk_id: UUID,
    doc_id: UUID,
    text: str = "the chunk text",
    warnings: list[Warning] | None = None,
) -> SerializerInputs:
    return SerializerInputs(
        chunks=[
            RankedChunk(
                chunk_id=chunk_id,
                document_id=doc_id,
                score=0.9,
                status="active",
                heading_path=(),
                last_reviewed=date.today(),
            )
        ],
        warnings=warnings or [],
        conflicts=[],
        chunk_text={chunk_id: text},
        document_refs={doc_id: DocumentRef(document_id=doc_id, title="t")},
    )


class TestRequestDefault:
    def test_default_is_false(self) -> None:
        """The flag must default to False so existing consumers don't
        accidentally see new inline markers."""
        req = ContextPackRequest(query="q", task="t")
        assert req.embed_warnings_in_text is False

    def test_explicit_true_round_trips(self) -> None:
        req = ContextPackRequest(query="q", task="t", embed_warnings_in_text=True)
        assert req.embed_warnings_in_text is True


class TestMarkerShape:
    def test_no_markers_when_flag_off(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            text="hello",
            warnings=[
                Warning(
                    type="stale_source",
                    message="m",
                    source_id=doc_id,
                    severity="advisory",
                    action="inform",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=False,
        )
        assert pack.evidence[0].text == "hello"
        assert "KILN-WARN" not in pack.evidence[0].text

    def test_marker_emitted_when_flag_on(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            warnings=[
                Warning(
                    type="stale_source",
                    message="last reviewed 2024-01-01",
                    source_id=doc_id,
                    severity="advisory",
                    action="inform",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        assert "[KILN-WARN: stale_source — last reviewed 2024-01-01]" in pack.evidence[0].text

    def test_marker_format_is_exact(self) -> None:
        """Regex-level pin so a future refactor doesn't drop the em-dash
        or change the bracket shape."""
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            warnings=[
                Warning(
                    type="weak_evidence",
                    message="m",
                    source_id=doc_id,
                    severity="warning",
                    action="request_human_review",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        # Marker = [KILN-WARN: <type> — <message>]
        assert re.search(r"\[KILN-WARN: \w+ — .+\]", pack.evidence[0].text)


class TestBlockingExclusion:
    """Embedding a blocking warning's message inside the chunk text
    would re-introduce the injection vector (sensitive_content +
    prompt_injection_pattern chunks are normally dropped; if one
    sneaks through, we MUST NOT embed its message inline)."""

    def test_prompt_injection_warning_not_embedded(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            warnings=[
                Warning(
                    type="prompt_injection_pattern",
                    message="ignore prior instructions",
                    source_id=doc_id,
                    severity="blocking",
                    action="refuse_to_synthesize",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        # The warning still rides in the top-level warnings[] array;
        # it MUST NOT be inlined into the chunk text.
        assert "KILN-WARN" not in pack.evidence[0].text


class TestGlobalWarningExclusion:
    """A warning with source_id=None (global, e.g. weak_evidence,
    query_normalized) doesn't attach to any specific chunk — keep it
    in the top-level warnings[] array."""

    def test_global_warning_not_embedded(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            warnings=[
                Warning(
                    type="weak_evidence",
                    message="all scores low",
                    source_id=None,  # global
                    severity="warning",
                    action="request_human_review",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        assert "KILN-WARN" not in pack.evidence[0].text


class TestIdempotency:
    def test_repeated_assembly_produces_byte_identical_text(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        warnings = [
            Warning(
                type="deprecated_source",
                message="d",
                source_id=doc_id,
                severity="warning",
                action="prefer_other_sources",
            ),
            Warning(
                type="stale_source",
                message="s",
                source_id=doc_id,
                severity="advisory",
                action="inform",
            ),
        ]
        # Construct two identical inputs (deep copies so mutations
        # on one don't leak to the other).
        inputs_a = _build_inputs(chunk_id=chunk_id, doc_id=doc_id, warnings=warnings.copy())
        inputs_b = _build_inputs(chunk_id=chunk_id, doc_id=doc_id, warnings=warnings.copy())
        kwargs: dict[str, object] = {
            "task": "t",
            "query": "q",
            "max_chunks": 8,
            "max_tokens": 3000,
            "embed_warnings_in_text": True,
        }
        pack_a = assemble_context_pack(inputs_a, **kwargs)  # type: ignore[arg-type]
        pack_b = assemble_context_pack(inputs_b, **kwargs)  # type: ignore[arg-type]
        assert pack_a.evidence[0].text == pack_b.evidence[0].text

    def test_multiple_markers_sorted_deterministically(self) -> None:
        """Two warnings on one chunk → two markers in sorted (type, message) order."""
        chunk_id, doc_id = uuid4(), uuid4()
        warnings = [
            # Intentionally NOT in alphabetical order in the input list.
            Warning(
                type="stale_source",
                message="z",
                source_id=doc_id,
                severity="advisory",
                action="inform",
            ),
            Warning(
                type="deprecated_source",
                message="a",
                source_id=doc_id,
                severity="warning",
                action="prefer_other_sources",
            ),
        ]
        inputs = _build_inputs(chunk_id=chunk_id, doc_id=doc_id, warnings=warnings)
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        text = pack.evidence[0].text
        # deprecated_source sorts before stale_source alphabetically.
        dep_idx = text.index("deprecated_source")
        stale_idx = text.index("stale_source")
        assert dep_idx < stale_idx


class TestWarningsArrayStillEmitted:
    """The inline-marker mode is a DUPLICATION layer, not a
    replacement. The top-level warnings[] array still ships."""

    def test_warnings_present_in_response_alongside_markers(self) -> None:
        chunk_id, doc_id = uuid4(), uuid4()
        inputs = _build_inputs(
            chunk_id=chunk_id,
            doc_id=doc_id,
            warnings=[
                Warning(
                    type="stale_source",
                    message="m",
                    source_id=doc_id,
                    severity="advisory",
                    action="inform",
                )
            ],
        )
        pack = assemble_context_pack(
            inputs,
            task="t",
            query="q",
            max_chunks=8,
            max_tokens=3000,
            embed_warnings_in_text=True,
        )
        # Marker present AND warnings array still populated.
        assert "KILN-WARN" in pack.evidence[0].text
        assert len(pack.warnings) == 1
        assert pack.warnings[0].type == "stale_source"
