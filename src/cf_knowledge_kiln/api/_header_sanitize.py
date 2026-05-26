"""Shared sanitizer for opaque inbound HTTP header values (#309).

The kiln has TWO opaque-string headers an agent can pass:

* ``X-Request-ID`` (PR #265) — correlation key.
* ``Idempotency-Key`` (PR #309 / this module's main consumer) —
  dedup handle for replay-safe retries.

Both need the same sanitization shape: trim, reject empty,
truncate to ``_MAX_LEN``, scrub anything outside
``[A-Za-z0-9._-]`` to ``_`` so a newline-injected value can't
fool a log scraper or pollute a DB row.

Sharing one helper means a future widening of the allowed
charset (e.g., to admit ``/`` for hierarchical keys) lands in
one place, and the two headers can't drift on what "valid"
means.
"""

from __future__ import annotations

import re

# Allowed chars match RFC 7230's ``token`` minus a few graphical
# punctuation marks the kiln has no need to admit. ``.`` lets us
# accept UUIDs + dot-segmented versioned ids (``kiln.untrusted.v1``);
# ``-`` and ``_`` cover the common UUID / snake-case shapes.
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")

# 200 chars matches the OpenTelemetry W3C trace-id family (32 hex)
# plus vendor prefixes, and is well under Stripe's 255-char
# Idempotency-Key cap. Single value so both headers share it.
_MAX_LEN = 200


def sanitize_opaque_header(value: str | None) -> str | None:
    """Trim + scrub + truncate an opaque header value; ``None`` if unusable.

    ``None`` return signals the caller should treat the inbound
    header as absent (the middleware path for ``X-Request-ID``
    generates a fresh UUID4; the handler path for
    ``Idempotency-Key`` falls back to current non-idempotent
    behavior).

    The truncation happens BEFORE scrubbing so a long value
    containing only legal chars doesn't get partially mangled by
    a trailing whitespace ban.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_LEN:
        value = value[:_MAX_LEN]
    # Replace anything outside the allowed set so a log scraper
    # can't be fooled by a newline-injected value.
    scrubbed = _SANITIZE_RE.sub("_", value)
    return scrubbed or None


__all__ = ["sanitize_opaque_header"]
