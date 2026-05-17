"""Coerce frontmatter values into JSON-serializable shapes.

YAML's ``safe_load`` happily returns native Python types — ``date``,
``datetime``, ``UUID``, ``set`` — none of which round-trip through
:func:`json.dumps`. Anything that lands in ``documents.metadata``
(JSONB column) has to be JSON-native or the upsert blows up at the
asyncpg layer with a confusing TypeError.

This helper walks dicts/lists recursively and converts each leaf:

* ``datetime`` / ``date`` → ISO-8601 string. ``datetime`` is a subclass
  of ``date``, so the datetime branch must check first — keep that
  order if you ever refactor.
* ``Decimal`` → ``float`` (numerics are already lossy in JSON; this
  is consistent with what most consumers expect).
* ``UUID`` → string.
* ``PurePath`` (and subclasses) → POSIX-style string. Frontmatter
  won't carry one but pipeline callers easily could.
* ``set`` / ``frozenset`` → sorted list (sorted so the same set hashes
  to the same JSON blob, which keeps ON CONFLICT updates stable).
* ``bytes`` / ``bytearray`` → utf-8 decode, hex fallback on invalid
  utf-8 (hex over base64: shorter, no padding, debuggable by eye).

Dict keys are coerced to ``str`` — YAML can produce int keys
(``2026: ...``) that JSON can't represent. Two keys that stringify
the same will collide silently; vanishingly unlikely in our inputs.

Anything else falls through unchanged. The caller is responsible for
making sure no truly opaque object survives — we don't want to
silently hide a real bug under ``str(...)``.

Cycle handling: ``yaml.safe_load`` output is acyclic, but
``_upsert_document`` accepts arbitrary metadata from any caller. A
self-referential dict would otherwise hit ``RecursionError``; we
guard with an id-set so cycles short-circuit to ``"<cycle>"``.

Pure function, no I/O. Tested alongside the chunker.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePath
from typing import Any
from uuid import UUID


def jsonify(value: Any) -> Any:
    """Recursively convert ``value`` to a JSON-serializable shape."""
    return _jsonify(value, set())


def _jsonify(value: Any, seen: set[int]) -> Any:
    # Containers participate in cycle detection; scalars don't need to.
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if id(value) in seen:
            return "<cycle>"
        seen = seen | {id(value)}
    if isinstance(value, dict):
        return {str(k): _jsonify(v, seen) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v, seen) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonify(v, seen) for v in value), key=_sort_key)
    # datetime is a subclass of date — datetime branch must run first.
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    return value


def _sort_key(value: Any) -> str:
    """Stable comparator that works for mixed-type sets after jsonify."""
    return repr(value)


__all__ = ["jsonify"]
