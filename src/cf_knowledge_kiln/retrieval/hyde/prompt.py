"""#332 — HyDE prompt template.

Single canonical template the engine uses to ask the generator for a
short pseudo-document. Declarative voice (the LLM writes "X does Y"
prose, not a Q-A pair), citation-free (we're priming the embedding
arm, not answering the user), capped at ~150 output tokens.

Why these choices:

* **Declarative voice** matches the prose shape of indexed docs.
  Embedding the pseudo-doc lands closer in vector space to the real
  doc that would answer the query, which is the whole point of HyDE.
* **Citation-free** — the model would hallucinate citations otherwise,
  and the embedding arm doesn't use them. Removing them tightens the
  output and saves tokens.
* **~150-token cap** — long enough to carry domain language, short
  enough that the embed cost stays trivial. ~150 tokens is roughly
  one paragraph; that's the typical real-doc chunk size we're trying
  to land near in vector space.
"""

from __future__ import annotations

HYDE_MAX_OUTPUT_TOKENS: int = 200
"""Generation cap. Slightly above the 150-token target so the model
has headroom to finish a sentence cleanly without truncation. The
embedding step doesn't care about the exact length."""


HYDE_PROMPT_TEMPLATE: str = """\
You are drafting a short technical-documentation paragraph that would \
answer the user's question. The paragraph will be embedded and used to \
search a documentation corpus — it is NOT shown to the user.

Rules:
- Write in declarative voice ("The system does X", "Y is configured by Z").
- One paragraph, roughly 100-150 words.
- Use the domain language the real documentation would use.
- Do NOT cite sources, do NOT use phrases like "according to" or "as documented".
- Do NOT include numbered lists, headings, or markdown formatting.
- If the question is ambiguous, draft the most-common interpretation.

Question:
{query}

Paragraph:"""


def render_prompt(query: str) -> str:
    """Substitute ``{query}`` in the canonical template.

    Public surface so the engine + tests share one rendering path; a
    future refactor that adds a system-prompt boundary or a few-shot
    set lives here, not inside the engine.
    """
    return HYDE_PROMPT_TEMPLATE.format(query=query.strip())


__all__ = ["HYDE_MAX_OUTPUT_TOKENS", "HYDE_PROMPT_TEMPLATE", "render_prompt"]
