"""Unit tests for the GeneratorProvider Protocol + MockGeneratorProvider (#192 Phase A).

Mirrors :mod:`tests.unit.test_ingestion_embedding_protocol`. No real
LLM is reached — the mock provider is deterministic and offline.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.generation import (
    GenerationResult,
    GeneratorProvider,
    MockGeneratorProvider,
)


class TestMockGeneratorProvider:
    def test_implements_protocol(self) -> None:
        """Mock satisfies the runtime-checkable Protocol."""
        mock = MockGeneratorProvider()
        assert isinstance(mock, GeneratorProvider)

    def test_provider_and_model_metadata(self) -> None:
        mock = MockGeneratorProvider(model="mock-2")
        assert mock.provider == "mock"
        assert mock.model == "mock-2"

    async def test_generate_returns_generation_result(self) -> None:
        mock = MockGeneratorProvider()
        result = await mock.generate("hello", max_tokens=64)
        assert isinstance(result, GenerationResult)
        assert "hello" in result.text  # default template echoes the prompt
        assert result.finish_reason == "stop"
        assert result.model == "mock-generator"

    async def test_custom_response_template_overrides_echo(self) -> None:
        mock = MockGeneratorProvider(response_template="REFUSED")
        result = await mock.generate("anything", max_tokens=16)
        assert result.text == "REFUSED"

    async def test_records_calls_for_assertion(self) -> None:
        """``mock.calls`` lets tests verify what reached the generator."""
        mock = MockGeneratorProvider()
        await mock.generate("q1", max_tokens=8, temperature=0.7, stop=["END"])
        await mock.generate("q2", max_tokens=16)
        assert len(mock.calls) == 2
        first = mock.calls[0]
        assert first["prompt"] == "q1"
        assert first["max_tokens"] == 8
        assert first["temperature"] == 0.7
        assert first["stop"] == ["END"]
        second = mock.calls[1]
        assert second["temperature"] == 0.0  # default
        assert second["stop"] == []  # default None coerced to empty list

    async def test_finish_reason_is_configurable(self) -> None:
        """Tests need to drive the refusal path."""
        mock = MockGeneratorProvider(finish_reason="content_filter")
        result = await mock.generate("x", max_tokens=4)
        assert result.finish_reason == "content_filter"

    async def test_token_counts_reported_when_set(self) -> None:
        mock = MockGeneratorProvider(prompt_tokens=10, completion_tokens=7)
        result = await mock.generate("x", max_tokens=4)
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 7
        assert result.total_tokens == 17

    async def test_token_counts_default_to_none(self) -> None:
        """Reflects what providers without usage info actually return."""
        mock = MockGeneratorProvider()
        result = await mock.generate("x", max_tokens=4)
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None

    async def test_aclose_is_a_no_op(self) -> None:
        mock = MockGeneratorProvider()
        # Should not raise.
        await mock.aclose()
        await mock.aclose()


class TestGenerationResultShape:
    """Lock the GenerationResult shape — downstream serializers depend on it."""

    def test_required_and_optional_fields(self) -> None:
        result = GenerationResult(
            text="answer",
            finish_reason="stop",
            model="m1",
        )
        assert result.text == "answer"
        assert result.finish_reason == "stop"
        assert result.model == "m1"
        # Tokens default to None.
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None

    def test_token_fields_round_trip(self) -> None:
        result = GenerationResult(
            text="a",
            finish_reason="length",
            model="m1",
            prompt_tokens=5,
            completion_tokens=3,
            total_tokens=8,
        )
        assert result.prompt_tokens == 5
        assert result.completion_tokens == 3
        assert result.total_tokens == 8

    def test_frozen(self) -> None:
        """GenerationResult is immutable — defends against accidental mutation."""
        result = GenerationResult(text="a", finish_reason="stop", model="m1")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError  # noqa: B017
            result.text = "b"  # type: ignore[misc]
