"""Ingest-time sensitive-content scanner (#100).

Mirrors the prompt-injection scanner pattern: scan chunks once at
ingest, stamp a boolean on ``document_chunks.metadata``, let retrieval
read ``O(1)`` per chunk. The downstream behavior:

* Retrieval emits a ``sensitive_content`` warning for any top-K chunk
  that's stamped.
* ``requires_human_review`` already treats ``sensitive_content`` as a
  trip wire — flag is True whenever the warning fires.
* The agent serializer drops sensitive chunks from the context-pack
  body entirely (per AGENTS.md: "Sensitive content is allowed to
  surface in human results with a redaction notice; agent context
  packs must drop it entirely.").

Public surface:

* :func:`load_patterns` — read ``config/security.yaml`` and return a
  compiled regex list. Falls back to ``[]`` if the file is absent.
* :func:`scan` — return ``{"matched_pattern": "<the pattern source>"}``
  if any pattern matches, else ``None``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CompiledPattern:
    """Original pattern string + compiled regex.

    We hand the *source* back as ``matched_pattern`` so an operator
    auditing the row can grep for it; the compiled form is for the
    scan loop.
    """

    source: str
    compiled: re.Pattern[str]


def load_patterns(path: str | Path) -> list[_CompiledPattern]:
    """Read ``content_filters.sensitive_patterns`` from a YAML config.

    Missing file → empty list (warn). Malformed YAML → raise. Empty or
    missing key → empty list (no warning; intentional config).
    Patterns that don't compile are dropped with a warning so a single
    typo doesn't poison every later call.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("no security config at %s; sensitive-content scanning disabled", p)
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML in {p}: {exc}") from exc
    content_filters = raw.get("content_filters") or {}
    patterns = content_filters.get("sensitive_patterns") or []
    if not isinstance(patterns, list):
        raise ValueError(
            f"{p}: content_filters.sensitive_patterns must be a list, got {type(patterns).__name__}"
        )
    out: list[_CompiledPattern] = []
    for pat in patterns:
        if not isinstance(pat, str) or not pat.strip():
            logger.warning("skipping non-string / empty sensitive pattern in %s: %r", p, pat)
            continue
        try:
            compiled = re.compile(pat)
        except re.error as exc:
            logger.warning("skipping invalid regex %r in %s: %s", pat, p, exc)
            continue
        out.append(_CompiledPattern(source=pat, compiled=compiled))
    return out


def scan(text: str, patterns: list[_CompiledPattern]) -> dict[str, Any] | None:
    """Return match info if any regex matches, else ``None``.

    Returns the source of the **first** matched pattern (not all of
    them). Empty ``patterns`` or empty ``text`` returns ``None``.
    """
    if not patterns or not text:
        return None
    for pat in patterns:
        if pat.compiled.search(text):
            return {"matched_pattern": pat.source}
    return None


__all__ = ["load_patterns", "scan"]
