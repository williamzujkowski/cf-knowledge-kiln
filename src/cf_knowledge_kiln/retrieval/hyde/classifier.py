"""#332 — HyDE gate: decide whether a query benefits from pseudo-doc expansion.

HyDE pays off most on queries the retriever finds hardest:

* **Short queries** — too few tokens for the vector arm to disambiguate
  (e.g. "offsite backup"); pseudo-doc adds context the user wouldn't
  spell out themselves.
* **Jargon-dense queries** — operator-speak that's all domain terms
  with no natural-language glue (e.g. "credhub ca rotation"); pseudo-
  doc fills in the surrounding sentences that exist in real docs.
* **Imperative-style queries** — "how to X", "explain Y", "what is Z"
  — these are explicit doc-lookup intents where the natural answer
  shape is a paragraph, which is exactly what HyDE generates.

Pure function. No I/O. No global state. Cheap to call on every query;
the expensive piece (the LLM generation) is gated by this.
"""

from __future__ import annotations

import re

# Imperative-shaped prefixes. Anchored to the start so "how can the
# backup fail" matches but "tell me how" doesn't (the latter is a
# chatty query that wouldn't shorten well).
_IMPERATIVE_PREFIX = re.compile(
    r"^\s*(how(\s+(do|to|can|does|should))?|"
    r"what(\s+is|\s+are|'s)?|"
    r"why(\s+does|\s+is)?|"
    r"when(\s+does|\s+is|\s+to)?|"
    r"where(\s+is|\s+are|\s+do)?|"
    r"which|"
    r"explain|"
    r"describe|"
    r"show\s+me|"
    r"tell\s+me\s+about|"
    r"can\s+I|"
    r"should\s+I)\b",
    re.IGNORECASE,
)

# Cheap-and-narrow jargon heuristic. A token is "jargon-like" if it:
#   * contains a non-alphanumeric character anywhere (kebab-case, dot,
#     underscore, slash → component / path / config names)
#   * is ALL-CAPS and ≥ 2 chars (acronyms: CF, DB, OIDC, OSBAPI)
#   * mixes case across the token (camelCase, PascalCase)
#   * is ≥ 12 chars long (unusually long word — likely a compound noun
#     specific to the domain)
#
# Conjugation / pluralization is irrelevant — we're not building a
# vocabulary; we're estimating how "natural-prose-shaped" the query
# looks. Misses are fine; the gate has other arms.
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_HAS_INNER_CASE_FLIP = re.compile(r"[a-z][A-Z]|[A-Z][a-z][a-z]*[A-Z]")


def _is_jargon_like(token: str) -> bool:
    if not token:
        return False
    if _NON_ALNUM.search(token):
        return True
    if len(token) >= 2 and token.isupper():
        return True
    if _HAS_INNER_CASE_FLIP.search(token):
        return True
    return len(token) >= 12


def jargon_density(query: str) -> float:
    """Fraction of tokens that look jargon-like. Returns 0.0 for empty input.

    Exposed so tests + future telemetry can audit the heuristic on a
    real query stream without re-implementing the math.
    """
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    if not tokens:
        return 0.0
    n_jargon = sum(1 for t in tokens if _is_jargon_like(t))
    return n_jargon / len(tokens)


def token_count(query: str) -> int:
    """Whitespace-split token count. Cheap proxy for tokenizer-based
    count; HyDE's gate doesn't need exact tokens, only "is this short"."""
    return len([t for t in re.split(r"\s+", query.strip()) if t])


def should_hyde(
    query: str,
    *,
    token_threshold: int = 8,
    jargon_density_threshold: float = 0.4,
) -> bool:
    """Decide whether the query is a HyDE candidate.

    Gate fires when ANY of:

    * Token count < ``token_threshold`` (short query → context-poor)
    * Jargon density > ``jargon_density_threshold`` (operator-speak)
    * Imperative prefix match (explicit doc-lookup intent)

    Returns False for empty / whitespace-only queries (no point expanding
    nothing) and for very-long chatty queries that already carry enough
    context for the vector arm.
    """
    cleaned = query.strip()
    if not cleaned:
        return False
    if token_count(cleaned) < token_threshold:
        return True
    if jargon_density(cleaned) > jargon_density_threshold:
        return True
    return bool(_IMPERATIVE_PREFIX.search(cleaned))


__all__ = ["jargon_density", "should_hyde", "token_count"]
