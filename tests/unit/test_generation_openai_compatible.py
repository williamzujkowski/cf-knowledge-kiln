"""Unit tests for the openai-compatible HTTP generator adapter (#192 Phase A).

Mirrors :mod:`tests.unit.test_ingestion_embedding_openai_compatible`.
Network calls are intercepted with ``httpx.MockTransport`` so the
suite stays hermetic.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cf_knowledge_kiln.generation.openai_compatible import (
    GeneratorRequestError,
    OpenAICompatibleGeneratorProvider,
)


def _chat_response(
    text: str,
    *,
    finish_reason: str = "stop",
    model: str = "test-gen",
    prompt_tokens: int | None = 5,
    completion_tokens: int | None = 3,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
    }
    if prompt_tokens is not None and completion_tokens is not None:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return body


def _make_provider(
    transport: httpx.MockTransport,
    *,
    api_key: str | None = "sk-test",
    concurrency: int = 4,
    max_retries: int = 2,
) -> OpenAICompatibleGeneratorProvider:
    client = httpx.AsyncClient(transport=transport, base_url="https://api.example.test")
    return OpenAICompatibleGeneratorProvider(
        client=client,
        model="test-gen",
        api_key=api_key,
        concurrency=concurrency,
        max_retries=max_retries,
        # Keep retry tests fast.
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
    )


class TestOpenAICompatibleGeneratorAdapter:
    async def test_generate_round_trip(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_chat_response("the answer"))

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            result = await provider.generate(
                "test prompt", max_tokens=64, temperature=0.3, stop=["END"]
            )
        finally:
            await provider.aclose()

        assert result.text == "the answer"
        assert result.finish_reason == "stop"
        assert result.model == "test-gen"
        assert result.prompt_tokens == 5
        assert result.completion_tokens == 3
        assert result.total_tokens == 8

        assert captured["url"] == "https://api.example.test/v1/chat/completions"
        assert captured["auth"] == "Bearer sk-test"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "test-gen"
        assert body["max_tokens"] == 64
        assert body["temperature"] == 0.3
        assert body["stop"] == ["END"]
        # Single-user-message shape.
        assert body["messages"] == [{"role": "user", "content": "test prompt"}]

    async def test_no_stop_means_no_stop_field_in_payload(self) -> None:
        """Don't send ``"stop": null`` — confuses some endpoints."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_chat_response("ok"))

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        body = captured["body"]
        assert isinstance(body, dict)
        assert "stop" not in body

    async def test_finish_reason_length_passed_through(self) -> None:
        """A length-truncated completion is a NON-error case for downstream."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response("truncated...", finish_reason="length"))

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            result = await provider.generate("p", max_tokens=4)
        finally:
            await provider.aclose()
        assert result.finish_reason == "length"
        assert result.text == "truncated..."

    async def test_content_filter_returns_empty_text_and_reason(self) -> None:
        """Provider refusal MUST surface a non-text result, not an exception."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_response("", finish_reason="content_filter"),
            )

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            result = await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        assert result.text == ""
        assert result.finish_reason == "content_filter"

    async def test_missing_usage_block_yields_none_token_counts(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_chat_response("ok", prompt_tokens=None, completion_tokens=None),
            )

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            result = await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None

    async def test_retries_on_429_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json=_chat_response("ok"))

        provider = _make_provider(httpx.MockTransport(handler), max_retries=2)
        try:
            result = await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        assert result.text == "ok"
        assert attempts["n"] == 3

    async def test_non_retryable_4xx_raises_immediately(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            with pytest.raises(GeneratorRequestError, match="status 400"):
                await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()

    async def test_raises_when_retries_exhausted_on_5xx(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream busy")

        provider = _make_provider(httpx.MockTransport(handler), max_retries=1)
        try:
            with pytest.raises(GeneratorRequestError, match="exhausted retries"):
                await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()

    async def test_transport_error_retries_then_raises(self) -> None:
        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ConnectError("simulated network down")

        provider = _make_provider(httpx.MockTransport(handler), max_retries=1)
        try:
            with pytest.raises(GeneratorRequestError, match="failed after"):
                await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        assert attempts["n"] == 2  # initial + 1 retry

    async def test_no_choices_in_response_raises(self) -> None:
        """A response body with empty ``choices`` is an upstream contract bug."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [], "model": "test-gen"})

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            with pytest.raises(GeneratorRequestError, match="no choices"):
                await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()

    async def test_no_api_key_does_not_inject_auth_header(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=_chat_response("ok"))

        provider = _make_provider(httpx.MockTransport(handler), api_key=None)
        try:
            await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        # No auth header attached.
        assert captured["auth"] is None

    async def test_from_url_constructs_client(self) -> None:
        """Smoke test the convenience constructor."""
        provider = OpenAICompatibleGeneratorProvider.from_url(
            base_url="https://api.example.test",
            model="test-gen",
            api_key="sk-test",
        )
        try:
            assert provider.model == "test-gen"
            assert provider.provider == "openai-compatible"
        finally:
            await provider.aclose()

    async def test_rejects_non_positive_max_tokens(self) -> None:
        provider = _make_provider(
            httpx.MockTransport(lambda r: httpx.Response(200, json=_chat_response("ok")))
        )
        try:
            with pytest.raises(ValueError, match="max_tokens"):
                await provider.generate("p", max_tokens=0)
            with pytest.raises(ValueError, match="max_tokens"):
                await provider.generate("p", max_tokens=-1)
        finally:
            await provider.aclose()

    def test_rejects_non_positive_concurrency(self) -> None:
        client = httpx.AsyncClient()
        try:
            with pytest.raises(ValueError, match="concurrency"):
                OpenAICompatibleGeneratorProvider(client=client, model="m", concurrency=0)
        finally:
            import asyncio

            asyncio.run(client.aclose())

    def test_rejects_negative_max_retries(self) -> None:
        client = httpx.AsyncClient()
        try:
            with pytest.raises(ValueError, match="max_retries"):
                OpenAICompatibleGeneratorProvider(client=client, model="m", max_retries=-1)
        finally:
            import asyncio

            asyncio.run(client.aclose())

    async def test_secrets_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """An API key passed to the provider must NEVER appear in logs.

        Mirrors the embedding adapter's contract — paranoia about
        accidentally leaking bearer tokens through structlog or stderr.
        """
        import logging

        attempts = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, text="busy")
            return httpx.Response(200, json=_chat_response("ok"))

        provider = _make_provider(
            httpx.MockTransport(handler),
            api_key="sk-leak-canary-token",
            max_retries=2,
        )
        try:
            with caplog.at_level(logging.DEBUG):
                await provider.generate("p", max_tokens=8)
        finally:
            await provider.aclose()
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "sk-leak-canary-token" not in joined
