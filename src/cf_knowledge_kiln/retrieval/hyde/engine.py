"""#332 — HyDE engine. Composes classifier + cache + generator into ``expand``.

Public surface:

* :class:`HydeResult` — return shape of :meth:`HydeEngine.expand`.
* :class:`HydeEngine.expand(query)` returns ``HydeResult``. Never raises.

The result's ``pseudo_doc`` is ``None`` (the engine declines to expand)
when ANY of:

* The classifier gate says no (query is long + low-jargon + non-imperative).
* No generator is configured (the operator turned HyDE off by passing
  ``generator=None`` — useful in test environments that don't want to
  spin up an LLM).
* The generator raised (network blip, content-filter, auth failure) —
  the engine logs and degrades to bare retrieval.
* The generator returned empty output (content filter, refusal, etc.).

Two metadata fields carry operational visibility for the
``retrieval.hyde`` OTel span (#404):

* ``cache_hit`` — true ONLY when the cache short-circuited the
  generator. All skip paths (classifier-off, no-generator, raised,
  empty-output) report ``False``.
* ``generation_ms`` — wall-clock duration of the generator call on a
  cache miss. ``None`` for every path that didn't actually call the
  generator (including raises — partial timings would mislead).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from cf_knowledge_kiln.generation import GeneratorProvider
from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache, cache_key
from cf_knowledge_kiln.retrieval.hyde.classifier import should_hyde
from cf_knowledge_kiln.retrieval.hyde.prompt import (
    HYDE_MAX_OUTPUT_TOKENS,
    render_prompt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HydeResult:
    """The expanded pseudo-doc plus span-bearing metadata.

    Frozen so a caller (typically the retrieval engine, immediately
    after the await) can't mutate the timing/cache facts before they
    land in the OTel span. ``pseudo_doc=None`` is the universal
    "skip HyDE" sentinel; the metadata fields then carry skip-safe
    defaults so the span doesn't have to special-case the absence
    of expansion.
    """

    pseudo_doc: str | None
    cache_hit: bool = False
    generation_ms: float | None = None


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

    async def expand(self, query: str) -> HydeResult:
        """Return a :class:`HydeResult` with the pseudo-doc + span
        metadata. ``pseudo_doc=None`` means HyDE should be skipped;
        the metadata still reflects what actually happened (cache hit,
        timing) so the OTel span can attribute the work honestly.

        Order of checks: classifier → no-generator → cache → generator.
        Each early-out short-circuits the downstream cost.
        """
        if not should_hyde(
            query,
            token_threshold=self._token_threshold,
            jargon_density_threshold=self._jargon_density_threshold,
        ):
            return HydeResult(pseudo_doc=None)
        if self._generator is None:
            # Operator-disabled. Logged at debug only; this is the
            # common shape in dev/test where no LLM is wired up.
            logger.debug("hyde: no generator configured; skipping expansion")
            return HydeResult(pseudo_doc=None)
        normalized = _normalize_for_cache(query)
        key = cache_key(self._generator.provider, self._generator.model, normalized)
        hit = self._cache.get(key)
        if hit is not None:
            return HydeResult(pseudo_doc=hit, cache_hit=True, generation_ms=None)
        # Wrap the generator call in a wall-clock measurement so the
        # span can attribute latency. ``perf_counter`` is monotonic,
        # and we measure around the await so the await + any internal
        # cancellation handling is included.
        start = time.perf_counter()
        try:
            result = await self._generator.generate(
                render_prompt(query),
                max_tokens=self._max_tokens,
                temperature=0.0,
            )
        except Exception:
            # Generator failure → degrade to bare retrieval. NEVER
            # let a HyDE error surface as a search failure. Drop the
            # partial timing too — a half-completed call's duration
            # would mislead an operator into tuning a timeout against
            # noise.
            logger.exception("hyde: generator raised; degrading to bare retrieval")
            return HydeResult(pseudo_doc=None)
        generation_ms = (time.perf_counter() - start) * 1000.0
        text = (result.text or "").strip()
        if not text:
            # Empty output (content filter, refusal). Don't cache;
            # the next call will re-attempt in case the upstream was
            # transient. The call DID happen and DID take time, but
            # operators tuning HyDE care about successful-generation
            # latency, not refusal-roundtrip latency — drop the
            # timing on the empty path too. (The empty-output event
            # is rare enough that operators look at logs, not spans.)
            logger.warning(
                "hyde: generator returned empty output (finish_reason=%s); "
                "degrading to bare retrieval",
                getattr(result, "finish_reason", "?"),
            )
            return HydeResult(pseudo_doc=None)
        self._cache.put(key, text)
        return HydeResult(pseudo_doc=text, cache_hit=False, generation_ms=generation_ms)


__all__ = ["HydeEngine", "HydeResult"]
