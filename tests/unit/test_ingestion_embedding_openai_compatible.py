"""Unit tests for the openai-compatible HTTP embedding adapter.

Issue: #17. Network calls are intercepted with ``httpx.MockTransport``
so the suite stays hermetic. The integration tier exercises a real
endpoint if ``KILN_EMBEDDING_INTEGRATION=1`` is set; that lives
separately in tests/integration/.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from cf_knowledge_kiln.ingestion.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)


def _embedding_response(dim: int, count: int) -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [0.1 * (i + 1)] * dim}
            for i in range(count)
        ],
        "model": "test-embed",
        "usage": {"prompt_tokens": count, "total_tokens": count},
    }


def _make_provider(
    transport: httpx.MockTransport,
    *,
    api_key: str = "sk-test",
    concurrency: int = 4,
    max_retries: int = 2,
    dimensions: int = 768,
) -> OpenAICompatibleEmbeddingProvider:
    client = httpx.AsyncClient(transport=transport, base_url="https://api.example.test")
    return OpenAICompatibleEmbeddingProvider(
        client=client,
        model="test-embed",
        dimensions=dimensions,
        api_key=api_key,
        concurrency=concurrency,
        max_retries=max_retries,
        # Keep retry tests fast; the 0.5s default is for real endpoints.
        base_backoff_seconds=0.001,
        max_backoff_seconds=0.01,
    )


class TestOpenAICompatibleAdapter:
    async def test_embed_round_trip(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_embedding_response(8, 2))

        provider = _make_provider(httpx.MockTransport(handler), dimensions=8)
        try:
            vectors = await provider.embed(["alpha", "beta"])
        finally:
            await provider.aclose()

        assert len(vectors) == 2
        assert all(len(v) == 8 for v in vectors)
        assert captured["url"] == "https://api.example.test/v1/embeddings"
        assert captured["auth"] == "Bearer sk-test"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "test-embed"
        assert body["input"] == ["alpha", "beta"]

    async def test_empty_input_skips_http_call(self) -> None:
        called = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=_embedding_response(8, 0))

        provider = _make_provider(httpx.MockTransport(handler))
        try:
            assert await provider.embed([]) == []
        finally:
            await provider.aclose()
        assert called is False

    async def test_retries_on_429_then_succeeds(self) -> None:
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=_embedding_response(4, 1))

        provider = _make_provider(httpx.MockTransport(handler), max_retries=3, dimensions=4)
        try:
            [vector] = await provider.embed(["hi"])
        finally:
            await provider.aclose()

        assert len(attempts) == 3
        assert len(vector) == 4

    async def test_retries_on_5xx_then_gives_up(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        provider = _make_provider(httpx.MockTransport(handler), max_retries=2)
        try:
            with pytest.raises(RuntimeError, match="503"):
                await provider.embed(["hi"])
        finally:
            await provider.aclose()

    async def test_4xx_does_not_retry(self) -> None:
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, json={"error": "bad request"})

        provider = _make_provider(httpx.MockTransport(handler), max_retries=5)
        try:
            with pytest.raises(RuntimeError, match="400"):
                await provider.embed(["hi"])
        finally:
            await provider.aclose()
        assert len(attempts) == 1

    async def test_dimensions_mismatch_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            # Configured dim is 8; server returns 16.
            return httpx.Response(200, json=_embedding_response(16, 1))

        provider = _make_provider(httpx.MockTransport(handler), dimensions=8)
        try:
            with pytest.raises(ValueError, match="dimensions"):
                await provider.embed(["hi"])
        finally:
            await provider.aclose()

    async def test_secrets_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="bad")

        provider = _make_provider(
            httpx.MockTransport(handler),
            api_key="sk-supersecret-do-not-log",  # pragma: allowlist secret
            max_retries=1,
        )
        with caplog.at_level(logging.DEBUG):
            try:
                with pytest.raises(RuntimeError):
                    await provider.embed(["hi"])
            finally:
                await provider.aclose()
        for record in caplog.records:
            assert "sk-supersecret-do-not-log" not in record.getMessage()

    async def test_concurrency_cap_holds_when_pressed(self) -> None:
        """Two parallel callers, cap=1, should serialize through the adapter."""
        in_flight = 0
        peak = 0
        gate = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            # Yield so a concurrent task can race in if the cap is broken.
            await gate.wait()
            in_flight -= 1
            return httpx.Response(200, json=_embedding_response(4, 1))

        provider = _make_provider(httpx.MockTransport(handler), concurrency=1, dimensions=4)
        try:
            task_a = asyncio.create_task(provider.embed(["a"]))
            task_b = asyncio.create_task(provider.embed(["b"]))
            # Let both tasks block on the semaphore / mock handler.
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(task_a, task_b)
        finally:
            await provider.aclose()
        assert peak == 1

    async def test_provider_and_model_metadata(self) -> None:
        provider = _make_provider(httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
        try:
            assert provider.provider == "openai-compatible"
            assert provider.model == "test-embed"
            assert provider.dimensions == 768
        finally:
            await provider.aclose()
