"""Tests for OTel Phase 2 retrieval-phase spans in ``HybridRetriever``.

These tests don't require a real OTel install or a real Postgres — they
swap the module-scoped ``_TRACER`` for a tiny recording shim and stub
out ``_fetch_candidates`` so the SQL path doesn't run. The point is to
verify the *wiring*: each public method opens the expected span tree
with the expected attributes, regardless of what backend is collecting
them.

The end-to-end "spans actually flow to an exporter when [otel] is
installed" assertion is covered by the OTel SDK's own contract — we
don't re-test it here.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from cf_knowledge_kiln.retrieval import context_pack as context_pack_module
from cf_knowledge_kiln.retrieval import engine as engine_module
from cf_knowledge_kiln.retrieval.config import RetrievalConfig
from cf_knowledge_kiln.retrieval.engine import HybridRetriever
from cf_knowledge_kiln.retrieval.types import RetrievalFilters


class _RecordingSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_attributes(self, attrs: dict[str, Any]) -> None:
        self.attrs.update(attrs)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def record_exception(self, exception: BaseException) -> None:
        return None


class _RecordingTracer:
    """Captures every ``start_as_current_span`` call (name + init attrs + span)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], _RecordingSpan]] = []

    @contextmanager
    def start_as_current_span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        span = _RecordingSpan()
        self.calls.append((name, dict(attributes or {}), span))
        yield span

    def span_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def get(self, name: str) -> _RecordingSpan:
        """Return the recorded span for ``name`` (last occurrence wins)."""
        for n, _, span in reversed(self.calls):
            if n == name:
                return span
        raise KeyError(name)

    def init_attrs(self, name: str) -> dict[str, Any]:
        for n, attrs, _ in reversed(self.calls):
            if n == name:
                return attrs
        raise KeyError(name)


def _build_retriever() -> HybridRetriever:
    """A HybridRetriever wired with mocks — _fetch_candidates is stubbed below."""
    mock_db = MagicMock()
    mock_provider = MagicMock()
    mock_provider.provider = "mock"
    mock_provider.model = "mock-model"
    mock_provider.dimensions = 8
    return HybridRetriever(
        db=mock_db,
        embedding_provider=mock_provider,
        config=RetrievalConfig(),
    )


@pytest.fixture
def recording_tracer(monkeypatch: pytest.MonkeyPatch) -> _RecordingTracer:
    tracer = _RecordingTracer()
    monkeypatch.setattr(engine_module, "_TRACER", tracer)
    # #402: context_pack orchestration was extracted into a sibling
    # module with its own module-scope _TRACER. Patch both so the
    # recorder sees the full span tree of the agent path too.
    monkeypatch.setattr(context_pack_module, "_TRACER", tracer)
    return tracer


@pytest.fixture
def stub_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_fetch_candidates`` with a no-row stub.

    We're testing the orchestration spans, not the SQL path. The SQL
    spans inside ``_run_query`` use the same tracer, so a separate
    test asserts those directly.
    """

    async def _fake(
        self: HybridRetriever,
        query: str,
        filters: RetrievalFilters,
        *,
        session: Any | None,
    ) -> list[Any]:
        return []

    monkeypatch.setattr(HybridRetriever, "_fetch_candidates", _fake)


async def test_search_emits_root_and_phase_spans(
    recording_tracer: _RecordingTracer, stub_fetch: None
) -> None:
    retriever = _build_retriever()
    await retriever.search(
        "test query",
        filters=RetrievalFilters(),
        max_results=5,
    )
    names = recording_tracer.span_names()
    assert "retrieval.search" in names
    assert "retrieval.normalize_query" in names
    assert "retrieval.apply_boosts" in names
    assert "retrieval.collect_warnings" in names


async def test_search_root_span_has_consumer_type_and_query_length(
    recording_tracer: _RecordingTracer, stub_fetch: None
) -> None:
    retriever = _build_retriever()
    await retriever.search(
        "hello world",
        filters=RetrievalFilters(),
        max_results=3,
    )
    init = recording_tracer.init_attrs("retrieval.search")
    assert init["retrieval.consumer_type"] == "human"
    assert init["retrieval.query_length"] == len("hello world")
    assert init["retrieval.max_results"] == 3
    root = recording_tracer.get("retrieval.search")
    assert root.attrs["retrieval.chunks_returned"] == 0
    # warnings_count is recorded but the exact value depends on which
    # warning emitters fire for the zero-row case (e.g. weak-evidence).
    # The contract we care about: the attribute is set, as an int.
    assert isinstance(root.attrs["retrieval.warnings_count"], int)


async def test_context_pack_emits_agent_consumer_and_agent_only_spans(
    recording_tracer: _RecordingTracer, stub_fetch: None
) -> None:
    retriever = _build_retriever()
    await retriever.context_pack(
        "agent query",
        task="answer the question",
        filters=RetrievalFilters(),
        max_chunks=4,
        max_tokens=1500,
    )
    names = recording_tracer.span_names()
    assert "retrieval.context_pack" in names
    # Spans shared with human path
    assert "retrieval.normalize_query" in names
    assert "retrieval.apply_boosts" in names
    # Agent-only spans
    assert "retrieval.detect_conflicts" in names
    assert "retrieval.assemble_context_pack" in names
    init = recording_tracer.init_attrs("retrieval.context_pack")
    assert init["retrieval.consumer_type"] == "agent"
    assert init["retrieval.max_chunks"] == 4
    assert init["retrieval.max_tokens"] == 1500


async def test_normalize_query_span_records_removed_phrases_count(
    recording_tracer: _RecordingTracer, stub_fetch: None
) -> None:
    """When the query has no prompt-injection markers, the attr is 0."""
    retriever = _build_retriever()
    await retriever.search(
        "plain query",
        filters=RetrievalFilters(),
        max_results=5,
    )
    norm = recording_tracer.get("retrieval.normalize_query")
    assert norm.attrs["retrieval.removed_phrases_count"] == 0


async def test_apply_boosts_span_records_counts(
    recording_tracer: _RecordingTracer, stub_fetch: None
) -> None:
    """With zero rows, chunks_in == chunks_kept == 0 — but both fields exist."""
    retriever = _build_retriever()
    await retriever.search(
        "boost test",
        filters=RetrievalFilters(),
        max_results=5,
    )
    boost = recording_tracer.get("retrieval.apply_boosts")
    assert boost.attrs["retrieval.chunks_in"] == 0
    assert boost.attrs["retrieval.chunks_kept"] == 0
