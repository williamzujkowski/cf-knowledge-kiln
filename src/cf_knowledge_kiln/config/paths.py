"""Config-file path resolution with example-file fallback (#241).

The kiln ships ``config/models.example.yaml`` and
``config/sources.example.yaml`` as templates — the non-example
filenames are gitignored so operators can customize without
committing secrets. But the settings defaults
(``models_config_path = "config/models.yaml"``,
``source_allowlist_path = "config/sources.yaml"``) point at filenames
that don't exist on a fresh checkout / fresh CF deploy. The result
was a startup that "worked" (the loader silently fell back to "no
embedding provider configured") but with the API responding
``embedding: not_configured`` on ``/readyz`` and the worker exiting
with ``source allowlist file not found``.

This module exposes :func:`resolve_with_example_fallback` so callers
can transparently fall back to ``<stem>.example<suffix>`` when the
configured filename is absent. A one-time-per-path log warning is
emitted so operators see the substitution without it spamming every
read.

Policy: out-of-the-box, the kiln runs against the shipped example
configs (mock embedding + empty source list) so a deploy is
inspectable + survives ``/healthz`` ``/readyz``. Operators MUST
override for production — the warning is the nudge.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


_FALLBACK_LOGGED: set[str] = set()
"""One log line per substituted path per process. Keys are the str()
of the *configured* (missing) path so the same fallback re-resolved
under repeated calls doesn't spam logs."""


def resolve_with_example_fallback(path: str | Path) -> Path:
    """Return ``path`` if it exists; else the ``.example`` sibling if it does.

    ``config/models.yaml`` → ``config/models.example.yaml``
    ``config/sources.yaml`` → ``config/sources.example.yaml``

    Returns the original (missing) ``path`` if neither exists — the
    caller's existing "file not found" handling fires unchanged, so
    this helper is strictly additive.

    Logs at WARNING level on substitution. The substitution is
    deliberately quiet on re-resolution (cached per path) so a
    long-running worker that re-reads the config on each poll doesn't
    flood the log with the same nudge.
    """
    p = Path(path)
    if p.exists():
        return p
    example = p.with_suffix(f".example{p.suffix}")
    if example.exists():
        key = str(p)
        if key not in _FALLBACK_LOGGED:
            _FALLBACK_LOGGED.add(key)
            logger.warning(
                "config file %s not found; falling back to %s. "
                "This is fine for first-deploy / local-dev inspection but "
                "operators should customize a non-example file in production.",
                p,
                example,
            )
        return example
    return p


__all__ = ["resolve_with_example_fallback"]
