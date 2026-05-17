"""Coerce frontmatter values into JSON-serializable shapes.

YAML's ``safe_load`` happily returns native Python types — ``date``,
``datetime``, ``UUID``, ``set`` — none of which round-trip through
:func:`json.dumps`. Anything that lands in ``documents.metadata``
(JSONB column) has to be JSON-native or the upsert blows up at the
asyncpg layer with a confusing TypeError.

This helper walks dicts/lists recursively and converts each leaf:

* ``date`` / ``datetime`` → ISO-8601 string (date alone uses date.isoformat()).
* ``Decimal`` → ``float`` (numerics are already lossy in JSON; this
  is consistent with what most consumers expect).
* ``UUID`` → string.
* ``set`` / ``frozenset`` → sorted list (sorted so the same set hashes
  to the same JSON blob, which keeps the ON CONFLICT updates stable).
* ``bytes`` / ``bytearray`` → utf-8 string with replacement, or hex
  fallback if the bytes aren't text.

Anything else falls through unchanged. The caller is responsible for
making sure no truly opaque object survives — we don't want to silently
hide a real bug under ``str(...)``.

Pure function, no I/O. Tested alongside the chunker.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def jsonify(value: Any) -> Any:
    """Recursively convert ``value`` to a JSON-serializable shape."""
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((jsonify(v) for v in value), key=_sort_key)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
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
