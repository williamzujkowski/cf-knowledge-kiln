"""HTTP embedding adapter speaking the OpenAI ``/v1/embeddings`` shape.

Works against any endpoint that honors the OpenAI embeddings contract:
the official API, vLLM, llama.cpp's openai-server, LM Studio, etc.
Selection is config — :mod:`cf_knowledge_kiln.ingestion.embedding.factory`
chooses this adapter when ``config/models.yaml`` names
``provider: openai-compatible`` for the active embedding model.

Concurrency is bounded by an ``asyncio.Semaphore`` whose size comes
from ``KILN_INGEST_CONCURRENCY``. Retries use exponential backoff with
small jitter on the retryable statuses (429, 5xx). Non-retryable 4xx
errors raise immediately. Secrets are never logged: the bearer token
is attached as a request header by the AsyncClient default headers,
not interpolated into log lines.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai-compatible"

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_CONCURRENCY = 4
DEFAULT_BASE_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_BACKOFF_SECONDS = 10.0


class OpenAICompatibleEmbeddingProvider:
    """Async embedding client for OpenAI-compatible endpoints.

    Owns its :class:`httpx.AsyncClient`; call :meth:`aclose` to release
    sockets. The factory does this for callers; tests do it explicitly.
    """

    provider = PROVIDER_NAME

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        endpoint_path: str = "/v1/embeddings",
    ) -> None:
        if dimensions <= 0:
            raise ValueError(f"dimensions must be positive, got {dimensions}")
        if concurrency <= 0:
            raise ValueError(f"concurrency must be positive, got {concurrency}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.model = model
        self.dimensions = dimensions
        self._client = client
        self._endpoint = endpoint_path
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        if api_key:
            # Attaching at client-level keeps the token out of per-call
            # log messages — httpx never prints default headers.
            self._client.headers["Authorization"] = f"Bearer {api_key}"

    @classmethod
    def from_url(
        cls,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> OpenAICompatibleEmbeddingProvider:
        """Construct the provider + its underlying ``httpx.AsyncClient``."""
        client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        return cls(
            client=client,
            model=model,
            dimensions=dimensions,
            api_key=api_key,
            concurrency=concurrency,
            max_retries=max_retries,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._semaphore:
            payload = {"model": self.model, "input": texts}
            response = await self._request_with_retry(payload)
        data = response.json()
        return self._parse_vectors(data, expected_count=len(texts))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        last_status: int | None = None
        last_body: str = ""
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._endpoint, json=payload)
            except httpx.TransportError as exc:
                logger.warning(
                    "embedding request transport error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc.__class__.__name__,
                )
                if attempt == self._max_retries:
                    raise RuntimeError(
                        f"embedding request failed after {attempt + 1} attempts: "
                        f"{exc.__class__.__name__}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue
            if response.status_code == 200:
                return response
            last_status = response.status_code
            last_body = response.text[:200]
            if response.status_code not in _RETRYABLE_STATUSES:
                # 4xx that isn't 408/425/429: client error, no point retrying.
                raise RuntimeError(
                    f"embedding endpoint returned {response.status_code}: {last_body}"
                )
            logger.warning(
                "embedding request retryable status %d (attempt %d/%d)",
                response.status_code,
                attempt + 1,
                self._max_retries + 1,
            )
            if attempt == self._max_retries:
                break
            await self._sleep_backoff(attempt)
        raise RuntimeError(
            f"embedding endpoint returned {last_status} after "
            f"{self._max_retries + 1} attempts: {last_body}"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self._base_backoff * (2**attempt), self._max_backoff)
        # 25% jitter avoids thundering-herd retries on shared endpoints.
        # Cryptographic randomness is overkill for backoff scheduling.
        jitter = random.uniform(0, delay * 0.25)  # noqa: S311  # nosec B311
        await asyncio.sleep(delay + jitter)

    def _parse_vectors(self, data: dict[str, Any], *, expected_count: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected_count:
            raise RuntimeError(
                f"embedding response missing or malformed 'data' (expected "
                f"{expected_count} items, got {len(items) if isinstance(items, list) else 'n/a'})"
            )
        # The server may return items out of order; honor the `index` field.
        ordered: list[list[float]] = [[] for _ in range(expected_count)]
        for item in items:
            idx = item.get("index")
            vec = item.get("embedding")
            if not isinstance(idx, int) or not isinstance(vec, list):
                raise RuntimeError("embedding response item missing index/embedding")
            if len(vec) != self.dimensions:
                raise ValueError(
                    f"embedding response has {len(vec)} dimensions, "
                    f"adapter is configured for {self.dimensions}"
                )
            ordered[idx] = [float(x) for x in vec]
        return ordered


__all__ = ["PROVIDER_NAME", "OpenAICompatibleEmbeddingProvider"]
