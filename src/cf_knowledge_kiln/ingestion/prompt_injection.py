"""Ingest-time prompt-injection scanner (#57).

Phase 5 retrieval needs to emit a ``prompt_injection_pattern`` warning
when a retrieved chunk contains any phrase from
``config.content_filters.prompt_injection_phrases``. The naive design
runs the pattern matcher against every retrieved chunk on every query
— ``O(N patterns * K chunks)`` on the hot path.

This module moves the cost to **ingest time**: scan each chunk once,
store the boolean on ``document_chunks.metadata``, let retrieval read
``O(1)`` per chunk. Pattern changes are rare; when they do change,
re-ingest is a known-cost operation.

Public surface:

* :func:`load_phrases` — read ``config/security.yaml`` and return the
  active phrase list. Falls back to an empty list if the file is
  absent (matching the "no security config = no warnings, but ingest
  still works" policy used elsewhere).
* :func:`scan` — return ``{"matched_pattern": "..."}`` if any phrase
  matches the text (case-insensitive substring), else ``None``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def load_phrases(path: str | Path) -> list[str]:
    """Read ``content_filters.prompt_injection_phrases`` from a YAML config.

    Missing file → empty list (warn). Malformed YAML → raise. Empty or
    missing key → empty list (no warning; intentional config). Any
    non-string entries are dropped with a warning so a typo doesn't
    poison every later call.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("no security config at %s; prompt-injection scanning disabled", p)
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML in {p}: {exc}") from exc
    content_filters = raw.get("content_filters") or {}
    phrases = content_filters.get("prompt_injection_phrases") or []
    if not isinstance(phrases, list):
        raise ValueError(
            f"{p}: content_filters.prompt_injection_phrases must be a list, got {type(phrases).__name__}"
        )
    cleaned: list[str] = []
    for phrase in phrases:
        if isinstance(phrase, str) and phrase.strip():
            cleaned.append(phrase)
        else:
            logger.warning(
                "skipping non-string / empty prompt-injection phrase in %s: %r", p, phrase
            )
    return cleaned


def scan(text: str, phrases: list[str]) -> dict[str, Any] | None:
    """Return match info if ``text`` contains any phrase, else ``None``.

    Case-insensitive substring match. Returns the **first** matched
    phrase (not all of them) — the chunk gets a single
    ``matched_pattern`` to put on its metadata; that's enough for the
    warning. If the caller wants all matches later, the contract can
    be extended without breaking existing readers.

    Empty ``phrases`` returns ``None`` immediately.
    """
    if not phrases or not text:
        return None
    normalized = text.lower()
    for phrase in phrases:
        if phrase.lower() in normalized:
            return {"matched_pattern": phrase}
    return None


__all__ = ["load_phrases", "scan"]
