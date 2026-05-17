"""Async Postgres connection layer.

Two responsibilities:

1. Resolve a SQLAlchemy URL from either an explicit setting
   (``KILN_DATABASE_URL``) or a Cloud Foundry service binding
   (``VCAP_SERVICES``).
2. Hold the async engine and expose a ``ping()`` for ``/readyz`` plus a
   session factory for repositories (Phase 2+).

This module does **not** create the engine on import. The
:class:`Database` lifecycle is owned by the FastAPI lifespan in
``cf_knowledge_kiln.api.app``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cf_knowledge_kiln.config import Settings

logger = logging.getLogger(__name__)

_ASYNC_SCHEME: Final = "postgresql+asyncpg"


def _normalize_dsn(url: str) -> str:
    """Return ``url`` as ``postgresql+asyncpg://...`` regardless of input scheme.

    Accepts ``postgres://``, ``postgresql://``, or already-async URLs.
    An unrecognized scheme is returned unchanged — the engine will fail
    at connect time with a clear SQLAlchemy error, which is preferable
    to silently guessing here.
    """
    if url.startswith(f"{_ASYNC_SCHEME}://"):
        return url
    for legacy in ("postgresql://", "postgres://"):
        if url.startswith(legacy):
            return f"{_ASYNC_SCHEME}://{url[len(legacy) :]}"
    return url


def _dsn_from_credentials(creds: dict[str, Any], service_name: str) -> str:
    """Build a Postgres DSN from VCAP credentials.

    Prefers a complete ``uri`` field; otherwise assembles from parts.
    Raises ``ValueError`` if no usable shape is present.
    """
    uri = creds.get("uri")
    if isinstance(uri, str) and uri:
        return _normalize_dsn(uri)
    required = ("host", "port", "username", "password", "database")
    if all(k in creds for k in required):
        host = creds["host"]
        port = creds["port"]
        user = creds["username"]
        password = creds["password"]
        database = creds["database"]
        return f"{_ASYNC_SCHEME}://{user}:{password}@{host}:{port}/{database}"
    raise ValueError(
        f"VCAP_SERVICES binding {service_name!r} has no usable Postgres credentials "
        f"(needs 'uri' or host/port/username/password/database)."
    )


def parse_vcap_services(vcap_json: str | None, service_name: str) -> str | None:
    """Extract an async Postgres DSN from a VCAP_SERVICES blob.

    Returns ``None`` if the env var is unset/empty or no binding matches
    ``service_name``. Raises ``ValueError`` if:

    * the blob is unparseable,
    * the matched binding has malformed credentials, OR
    * more than one binding shares ``service_name`` across labels (CF
      allows this; we refuse to guess which one the operator meant).
    """
    if not vcap_json:
        return None
    try:
        parsed = json.loads(vcap_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"VCAP_SERVICES is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("VCAP_SERVICES must be a JSON object keyed by service label.")
    matches: list[dict[str, Any]] = []
    for bindings in parsed.values():
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if isinstance(binding, dict) and binding.get("name") == service_name:
                matches.append(binding)
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"VCAP_SERVICES contains {len(matches)} bindings named {service_name!r}; "
            f"expected exactly one. Rename or remove the duplicates."
        )
    creds = matches[0].get("credentials")
    if not isinstance(creds, dict):
        raise ValueError(f"VCAP_SERVICES binding {service_name!r} is missing 'credentials'.")
    return _dsn_from_credentials(creds, service_name)


_REDACTED = "***"


def redact_dsn(url: str | None) -> str:
    """Return ``url`` with the password component replaced by ``***``.

    Phase 4 attaches the embedding API key at httpx-client level so it
    never reaches log messages; this is the peer for the database URL.
    Used by the worker + API startup log lines so an operator can see
    *which* DB the process is bound to without leaking the password to
    log aggregation.

    Handles:
    * URL-encoded passwords (no leak through encoding round-trip).
    * No-password DSNs (returned unchanged).
    * IPv6 hosts (``[::1]`` is preserved).
    * Strings the SQLAlchemy URL parser refuses (returned unchanged —
      callers should still avoid logging the original, but we don't
      have a safer answer when we can't parse).
    * ``None`` (returned as ``"<none>"`` so log lines stay readable).
    """
    if url is None:
        return "<none>"
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        # Not a URL we recognize; the caller will still need to decide
        # whether logging the raw value is safe.
        return url
    if parsed.password is None:
        return url
    return str(parsed.set(password=_REDACTED))


def resolve_database_url(settings: Settings) -> str | None:
    """Return the async DSN to use, or ``None`` if no source is configured.

    Precedence: ``KILN_DATABASE_URL`` (from settings) > ``VCAP_SERVICES``
    binding named by ``settings.pg_service_name``.
    """
    if settings.database_url:
        return _normalize_dsn(settings.database_url)
    return parse_vcap_services(os.environ.get("VCAP_SERVICES"), settings.pg_service_name)


class Database:
    """Owns the async engine and exposes a session factory + health ping."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
    ) -> None:
        logger.info(
            "starting Postgres engine: %s (pool_size=%d, max_overflow=%d)",
            redact_dsn(url),
            pool_size,
            max_overflow,
        )
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session(self) -> AsyncSession:
        """Return a fresh ``AsyncSession``. Caller is responsible for closing it."""
        return self._sessionmaker()

    async def ping(self) -> bool:
        """Return True iff a ``SELECT 1`` round-trip succeeds.

        Failures are logged at WARNING (with traceback) so operators can
        see misconfiguration without grepping uvicorn's stderr — but the
        exception is *not* re-raised: ``/readyz`` reports ``failing``
        and CF retains the route. Re-raising would 500 the probe and
        deregister the route on the foundation.
        """
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            logger.warning("Postgres readiness ping failed", exc_info=True)
            return False
        return True

    async def dispose(self) -> None:
        """Close the underlying pool. Idempotent."""
        await self._engine.dispose()
