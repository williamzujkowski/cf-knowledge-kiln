"""Unit tests for db.redact_dsn (#50).

The DB password lives in env vars / VCAP bindings; once it's inside a
DSN string it's easy to leak via log lines or tracebacks. Phase 4
took the same care with the embedding API key (attached at httpx
client level so it never reaches ``%s``-style log lines); this is
the peer for the DB.

Contract: the password is replaced by ``***``; everything else
(scheme, user, host, port, db, query string) is preserved.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.db import redact_dsn


def test_redacts_password_in_plain_dsn() -> None:
    url = "postgresql+asyncpg://kiln:supersecret@db.example:5432/kiln"  # pragma: allowlist secret
    redacted = redact_dsn(url)
    assert "supersecret" not in redacted
    assert "***" in redacted
    assert redacted == "postgresql+asyncpg://kiln:***@db.example:5432/kiln"


def test_redacts_url_encoded_password() -> None:
    """A %40-encoded password ('p@ss' → 'p%40ss') must not survive."""
    url = "postgresql+asyncpg://kiln:p%40ss%21w0rd@host:5432/db"  # pragma: allowlist secret
    redacted = redact_dsn(url)
    assert "p%40ss" not in redacted
    assert "p@ss" not in redacted
    assert "w0rd" not in redacted
    assert "***" in redacted


def test_preserves_no_password_dsn_unchanged() -> None:
    url = "postgresql+asyncpg://kiln@host:5432/db"
    assert redact_dsn(url) == url


def test_handles_ipv6_host() -> None:
    url = "postgresql+asyncpg://kiln:secret@[::1]:5432/db"  # pragma: allowlist secret
    redacted = redact_dsn(url)
    assert "secret" not in redacted
    assert "[::1]" in redacted
    assert "5432" in redacted


def test_handles_query_string() -> None:
    url = (
        "postgresql+asyncpg://kiln:secret@host:5432/db?sslmode=require"  # pragma: allowlist secret
    )
    redacted = redact_dsn(url)
    assert "secret" not in redacted
    assert "sslmode=require" in redacted


def test_returns_string_unchanged_when_unparseable() -> None:
    """Defensive fallback: a non-URL string is returned as-is."""
    assert redact_dsn("not-a-url-at-all") == "not-a-url-at-all"


def test_handles_none() -> None:
    """None gets a readable placeholder so log lines stay parseable."""
    assert redact_dsn(None) == "<none>"


def test_idempotent_on_already_redacted_url() -> None:
    """Running redact_dsn twice yields the same result."""
    url = "postgresql+asyncpg://kiln:supersecret@host:5432/db"  # pragma: allowlist secret
    once = redact_dsn(url)
    twice = redact_dsn(once)
    assert once == twice
    assert "supersecret" not in twice


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://kiln:pw@host/db",  # pragma: allowlist secret
        "postgres://kiln:pw@host/db",  # pragma: allowlist secret
    ],
)
def test_redacts_across_url_schemes(url: str) -> None:
    """The helper works regardless of which Postgres scheme the URL uses."""
    redacted = redact_dsn(url)
    assert "pw" not in redacted
    assert "***" in redacted
