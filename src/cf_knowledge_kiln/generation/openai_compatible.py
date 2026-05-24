"""HTTP generator adapter speaking the OpenAI ``/v1/chat/completions`` shape.

Mirrors :mod:`cf_knowledge_kiln.ingestion.embedding.openai_compatible` —
works against any endpoint that honors the OpenAI chat-completions
contract: the official API, vLLM, llama.cpp's openai-server, LM Studio,
Ollama, anything else with a compatible facade.

Selection is config — :mod:`cf_knowledge_kiln.generation.factory`
chooses this adapter when ``config/models.yaml::models.generator``
names ``provider: openai-compatible``.

Concurrency is bounded by a small semaphore (default 4; we don't
expect bursty parallel generation in /v1/answer). Retries use
exponential backoff with small jitter on the retryable statuses
(429, 5xx). Non-retryable 4xx errors raise immediately. Secrets are
never logged.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from cf_knowledge_kiln.generation import GenerationResult

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai-compatible"

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_CONCURRENCY = 4
DEFAULT_BASE_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_BACKOFF_SECONDS = 10.0


class GeneratorRequestError(RuntimeError):
    """Raised when the upstream generator endpoint refuses or fails terminally."""


class OpenAICompatibleGeneratorProvider:
    """Async LLM-synthesis client for OpenAI-shaped chat-completions endpoints.

    Owns its :class:`httpx.AsyncClient`; call :meth:`aclose` to release
    sockets. The factory does this for callers; tests do it explicitly.
    """

    provider = PROVIDER_NAME

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        model: str,
        api_key: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        endpoint_path: str = "/v1/chat/completions",
    ) -> None:
        if concurrency <= 0:
            raise ValueError(f"concurrency must be positive, got {concurrency}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.model = model
        self._client = client
        self._endpoint = endpoint_path
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        if api_key:
            # Attach at client-level so the token never appears in any
            # per-call log line — httpx omits default headers from its
            # debug output. Mirrors the embedding adapter's pattern.
            self._client.headers["Authorization"] = f"Bearer {api_key}"

    @classmethod
    def from_url(
        cls,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> OpenAICompatibleGeneratorProvider:
        """Construct the provider + its underlying ``httpx.AsyncClient``."""
        client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        return cls(
            client=client,
            model=model,
            api_key=api_key,
            concurrency=concurrency,
            max_retries=max_retries,
        )

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> GenerationResult:
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        # Use a single user message — /v1/answer composes the full
        # synthesis prompt (system rules + evidence + question) into one
        # string before calling. Splitting into system/user shapes is a
        # later optimization if specific providers reward it.
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = list(stop)
        async with self._semaphore:
            response = await self._request_with_retry(payload)
        data = response.json()
        return self._parse_completion(data)

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
                    "generation request transport error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    exc.__class__.__name__,
                )
                if attempt == self._max_retries:
                    raise GeneratorRequestError(
                        f"generation request failed after {attempt + 1} attempts: "
                        f"{exc.__class__.__name__}"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue
            if response.status_code == 200:
                return response
            last_status = response.status_code
            last_body = response.text[:200]
            if response.status_code not in _RETRYABLE_STATUSES:
                raise GeneratorRequestError(
                    f"generation request failed with status {last_status}: {last_body}"
                )
            if attempt == self._max_retries:
                break
            logger.warning(
                "generation request retryable status %d (attempt %d/%d)",
                last_status,
                attempt + 1,
                self._max_retries + 1,
            )
            await self._sleep_backoff(attempt)
        raise GeneratorRequestError(
            f"generation request exhausted retries; last status {last_status}: {last_body}"
        )

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self._base_backoff * (2**attempt), self._max_backoff)
        # Tiny jitter so concurrent retries don't synchronize.
        # Cryptographic randomness is overkill for backoff scheduling.
        delay += random.random() * 0.1  # noqa: S311  # nosec B311
        await asyncio.sleep(delay)

    def _parse_completion(self, data: dict[str, Any]) -> GenerationResult:
        """Pull the standard fields out of an OpenAI-shaped chat response.

        Returns an empty-text result with the provider's ``finish_reason``
        when the choice has no content — preserves the signal so the
        /v1/answer handler can render a refusal rather than crashing.
        """
        choices = data.get("choices") or []
        if not choices:
            raise GeneratorRequestError(
                "generator returned no choices; response body shape was unexpected"
            )
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish_reason = choice.get("finish_reason") or "stop"
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        # Some providers echo a different model than was requested
        # (routing layers, fallback hosts). Honor whatever they tell us;
        # the caller can compare against the configured ``model`` if it
        # cares about drift.
        actual_model = data.get("model") or self.model
        return GenerationResult(
            text=text,
            finish_reason=finish_reason,
            model=actual_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


__all__ = [
    "DEFAULT_BASE_BACKOFF_SECONDS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROVIDER_NAME",
    "GeneratorRequestError",
    "OpenAICompatibleGeneratorProvider",
]
