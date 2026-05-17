"""Deterministic token counting for chunk sizing.

Uses tiktoken's ``cl100k_base`` encoding by default (the OpenAI 4-family
tokenizer). Token counts are approximate when the active generator/
embedder isn't an OpenAI model, but the plan accepts approximate
counts — what we need is a *deterministic* ruler so chunk boundaries
don't drift between runs.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

DEFAULT_ENCODING = "cl100k_base"


@lru_cache(maxsize=1)
def _encoding(name: str) -> tiktoken.Encoding:
    """Cache one encoding at a time. Each Encoding is ~5-50 MB resident; a
    bigger cache inflates the worker footprint without a payoff (we only
    use ``cl100k_base`` today)."""
    return tiktoken.get_encoding(name)


def count_tokens(text: str, encoding: str = DEFAULT_ENCODING) -> int:
    """Return the number of tokens in ``text`` for ``encoding``."""
    return len(_encoding(encoding).encode(text))
