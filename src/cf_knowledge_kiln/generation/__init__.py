"""LLM-synthesis (generator) abstraction (#192 Phase A).

Mirrors :mod:`cf_knowledge_kiln.ingestion.embedding`: a swappable
``GeneratorProvider`` Protocol + a deterministic ``MockGeneratorProvider``
for tests. Real adapters live in sibling modules:

* :mod:`cf_knowledge_kiln.generation.openai_compatible` — HTTP adapter
  for ``/v1/chat/completions``-shaped endpoints (Phi-4, Llama-family,
  Anthropic via proxy, anything else that speaks the OpenAI-shaped API).
* A local in-process generator is intentionally **not** part of Phase A;
  hosting an LLM in-process is heavier than embedding and most ops
  prefer a separate inference endpoint anyway.

The factory in :mod:`cf_knowledge_kiln.generation.factory` picks one
based on ``config/models.yaml::models.generator``. Selection is config,
not code (mirrors ADR-0005).

Protocol contract
-----------------

* ``generate(prompt, *, max_tokens, ...) -> GenerationResult`` — async,
  returns a single text completion plus honest token/finish-reason
  metadata. No streaming in Phase A (added later if `/v1/answer` callers
  need it; non-streaming is simpler and fits the standard JSON response
  shape).
* ``provider`` / ``model`` — strings persisted alongside any
  generation telemetry so an audit can tie a synthesized answer back
  to the generator that produced it.
* ``aclose()`` — release HTTP / client resources. The mock is a no-op.

The :class:`GenerationResult` shape is deliberately minimal — text,
finish_reason, optional token counts, model. Anything richer
(logprobs, tool calls, function calls) is out of scope for the
``/v1/answer`` endpoint, which only needs a cited natural-language
answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationResult:
    """One LLM completion from a :class:`GeneratorProvider`.

    Attributes
    ----------
    text:
        The generated completion. Empty string if the provider returned
        no content (rare; typically means a content filter tripped on
        the provider side — see ``finish_reason``).
    finish_reason:
        Why generation stopped. The canonical OpenAI-shaped values are
        ``"stop"`` (natural completion or stop sequence hit),
        ``"length"`` (max_tokens hit), ``"content_filter"`` (provider
        refused), ``"tool_calls"`` (model decided to call a tool —
        unused here). We keep the string raw so future values from
        downstream providers surface unchanged.
    prompt_tokens / completion_tokens / total_tokens:
        Honest counts when the provider returns a ``usage`` block;
        ``None`` otherwise. The /v1/answer endpoint reports these so an
        operator can audit cost without re-tokenizing.
    model:
        What the provider actually used. Differs from
        :attr:`GeneratorProvider.model` only when the backend silently
        substitutes (e.g. a routing layer falling back). Persisted on
        any telemetry row so an audit can spot drift.
    """

    text: str
    finish_reason: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@runtime_checkable
class GeneratorProvider(Protocol):
    """Swappable LLM-synthesis backend.

    Implementations: :class:`MockGeneratorProvider`,
    ``OpenAICompatibleGeneratorProvider``.
    """

    provider: str
    model: str

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> GenerationResult: ...

    async def aclose(self) -> None: ...


class MockGeneratorProvider:
    """Deterministic in-process generator for tests.

    Returns ``response_template.format(prompt=prompt)`` — the prompt
    text is echoed into the output by default so tests can assert
    "the prompt actually reached the generator". Override via
    constructor for fixed-output cases (e.g. testing a refusal path).

    No network, no model weights. ``aclose()`` is a no-op.
    """

    provider: str = "mock"

    def __init__(
        self,
        *,
        model: str = "mock-generator",
        response_template: str = "MOCK[{prompt}]",
        finish_reason: str = "stop",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        self.model = model
        self._template = response_template
        self._finish_reason = finish_reason
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> GenerationResult:
        self.calls.append(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stop": list(stop or []),
            }
        )
        text = self._template.format(prompt=prompt)
        total: int | None = None
        if self._prompt_tokens is not None and self._completion_tokens is not None:
            total = self._prompt_tokens + self._completion_tokens
        return GenerationResult(
            text=text,
            finish_reason=self._finish_reason,
            model=self.model,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            total_tokens=total,
        )

    async def aclose(self) -> None:
        return None


# Re-export concrete providers so callers don't have to know which
# submodule a provider lives in. The factory remains the canonical
# selection entry point.
from cf_knowledge_kiln.generation.openai_compatible import (  # noqa: E402
    OpenAICompatibleGeneratorProvider,
)

__all__ = [
    "GenerationResult",
    "GeneratorProvider",
    "MockGeneratorProvider",
    "OpenAICompatibleGeneratorProvider",
]
