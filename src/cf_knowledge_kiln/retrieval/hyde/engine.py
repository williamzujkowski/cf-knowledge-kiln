"""#332 — HyDE engine. Composes classifier + cache + generator into ``expand``.

Public surface:

* :class:`HydeEngine.expand(query)` returns ``str | None``. Never raises.

Returns ``None`` (the engine declines to expand) when ANY of:

* The classifier gate says no (query is long + low-jargon + non-imperative).
* No generator is configured (the operator turned HyDE off by passing
  ``generator=None`` — useful in test environments that don't want to
  spin up an LLM).
* The generator raised (network blip, content-filter, auth failure) —
  the engine logs and degrades to bare retrieval.
* The generator returned empty output (content filter, refusal, etc.).

The engine is intentionally narrow — wiring it into
:class:`HybridRetriever` lives in #333. This module is the substrate;
production code paths don't consume it yet.
"""

from __future__ import annotations

import logging

from cf_knowledge_kiln.generation import GeneratorProvider
from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache, cache_key
from cf_knowledge_kiln.retrieval.hyde.classifier import should_hyde
from cf_knowledge_kiln.retrieval.hyde.prompt import (
    HYDE_MAX_OUTPUT_TOKENS,
    render_prompt,
)

logger = logging.getLogger(__name__)


def _normalize_for_cache(query: str) -> str:
    """Cheap normalization for cache keys. Lowercased + whitespace-collapsed.

    Equality here is "different surface, same meaning"-ish. A future
    pass could share more with retrieval.query_normalization but the
    current contract (case + whitespace only) covers the dominant
    user-typed variance without coupling to the security-driven
    normalization there.
    """
    return " ".join(query.strip().lower().split())


class HydeEngine:
    """Decide → cache → generate. Never raises; ``None`` means "skip HyDE."

    Construct once per process (the cache is per-instance). The
    classifier params + max-tokens are pinned at construction; a future
    config-driven retune lives at the call site.
    """

    def __init__(
        self,
        *,
        generator: GeneratorProvider | None,
        cache: HydeCache,
        token_threshold: int = 8,
        jargon_density_threshold: float = 0.4,
        max_tokens: int = HYDE_MAX_OUTPUT_TOKENS,
    ) -> None:
        self._generator = generator
        self._cache = cache
        self._token_threshold = token_threshold
        self._jargon_density_threshold = jargon_density_threshold
        self._max_tokens = max_tokens

    async def expand(self, query: str) -> str | None:
        """Return a pseudo-document, or ``None`` if HyDE should be skipped.

        Order of checks: classifier → no-generator → cache → generator.
        Each early-out short-circuits the downstream cost.
        """
        if not should_hyde(
            query,
            token_threshold=self._token_threshold,
            jargon_density_threshold=self._jargon_density_threshold,
        ):
            return None
        if self._generator is None:
            # Operator-disabled. Logged at debug only; this is the
            # common shape in dev/test where no LLM is wired up.
            logger.debug("hyde: no generator configured; skipping expansion")
            return None
        normalized = _normalize_for_cache(query)
        key = cache_key(self._generator.provider, self._generator.model, normalized)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            result = await self._generator.generate(
                render_prompt(query),
                max_tokens=self._max_tokens,
                temperature=0.0,
            )
        except Exception:
            # Generator failure → degrade to bare retrieval. NEVER
            # let a HyDE error surface as a search failure.
            logger.exception("hyde: generator raised; degrading to bare retrieval")
            return None
        text = (result.text or "").strip()
        if not text:
            # Empty output (content filter, refusal). Don't cache;
            # the next call will re-attempt in case the upstream was
            # transient.
            logger.warning(
                "hyde: generator returned empty output (finish_reason=%s); "
                "degrading to bare retrieval",
                getattr(result, "finish_reason", "?"),
            )
            return None
        self._cache.put(key, text)
        return text


__all__ = ["HydeEngine"]
