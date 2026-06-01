"""HyDE substrate (#332). See child modules for details.

* :mod:`.classifier` — :func:`should_hyde` gate.
* :mod:`.cache` — :class:`HydeCache` TTL+LRU store.
* :mod:`.prompt` — canonical generation prompt.
* :mod:`.engine` — :class:`HydeEngine` orchestrator.

Wiring into :class:`HybridRetriever` lives in #333. Nothing in
production retrieval consumes this yet.
"""

from __future__ import annotations

from cf_knowledge_kiln.retrieval.hyde.cache import HydeCache, cache_key
from cf_knowledge_kiln.retrieval.hyde.classifier import (
    jargon_density,
    should_hyde,
    token_count,
)
from cf_knowledge_kiln.retrieval.hyde.engine import HydeEngine, HydeResult
from cf_knowledge_kiln.retrieval.hyde.prompt import (
    HYDE_MAX_OUTPUT_TOKENS,
    HYDE_PROMPT_TEMPLATE,
    render_prompt,
)

__all__ = [
    "HYDE_MAX_OUTPUT_TOKENS",
    "HYDE_PROMPT_TEMPLATE",
    "HydeCache",
    "HydeEngine",
    "HydeResult",
    "cache_key",
    "jargon_density",
    "render_prompt",
    "should_hyde",
    "token_count",
]
