"""Programmatic Alembic ``upgrade head`` for app startup (#244).

The CF deploy pain: kiln ships Alembic migrations under
``alembic/versions/`` but no auto-apply on startup. First deploy
required an operator to ``cf ssh`` (via the lifecycle launcher) and
run ``make migrate`` before either app could serve a request — the
API 500'd on ``relation "alembic_version" does not exist`` until
that step ran. homelab-iac#702 + #715 wired the manual step into the
deploy script as a workaround.

This module exposes :func:`run_upgrade_head` (async) and
:func:`run_upgrade_head_sync` (sync). Both invoke
``alembic.command.upgrade(cfg, "head")`` against the configured DB
URL, with two important behaviors:

1. **Env-var + logger snapshot/restore.** ``alembic/env.py`` reads
   ``KILN_DATABASE_URL`` via ``resolve_database_url(get_settings())``
   and calls ``logging.config.fileConfig(...)`` with the default
   ``disable_existing_loggers=True``. Both have observable side
   effects in the calling process. The helper snapshots ambient state
   before the upgrade and restores it after, so callers that pass an
   override URL don't have their env or logging silently changed.

2. **Async wrapper.** ``alembic.command.upgrade`` is synchronous but
   internally drives an async engine via ``asyncio.run(...)``, which
   raises if called from inside a running event loop. The async
   wrapper runs the sync helper via :func:`asyncio.to_thread` so the
   API lifespan / worker ``serve()`` coroutines can call it without
   tripping over the nested-loop guard.

The plan agent's design doc is in
``tests/integration/_migration_isolation.py`` (#154) — that module
captured the env+logger isolation logic for the test suite first;
this module is the production-side mirror.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


_DB_URL_ENV = "KILN_DATABASE_URL"


def _alembic_ini_path() -> Path | str:
    """Resolve ``alembic.ini`` across editable + wheel-installed layouts.

    Two install shapes the kiln runs in:

    1. **Editable / source tree** (local dev, integration tests, a
       buildpack stage that hasn't copied to site-packages yet).
       ``__file__`` is ``src/cf_knowledge_kiln/db/migrations.py``, so
       ``parents[3]`` is the repo root and ``alembic.ini`` is right
       there next to ``pyproject.toml``.
    2. **Non-editable / wheel-installed** (Cloud Foundry's
       ``pip install .`` from ``requirements.txt`` — see #238 for why
       it's non-editable). ``__file__`` is
       ``site-packages/cf_knowledge_kiln/db/migrations.py``, so
       ``parents[3]`` is ``site-packages``'s parent (typically
       ``python3.12/``) — no ``alembic.ini`` there. The CF buildpack
       still has the SOURCE tree at ``/home/vcap/app`` (the CWD when
       ``./scripts/start-api.sh`` execs), so the cwd-relative string
       ``alembic.ini`` resolves correctly via Alembic's own search.

    Try the file-relative path first. If it exists, use it (handles
    both editable installs AND the test suite running from any CWD).
    Otherwise fall back to the literal string ``"alembic.ini"``,
    which Alembic resolves relative to CWD — the right answer in
    CF's wheel-installed layout where the source tree at
    ``/home/vcap/app`` still carries the file.
    """
    file_relative = Path(__file__).resolve().parents[3] / "alembic.ini"
    if file_relative.exists():
        return file_relative
    return "alembic.ini"


def _snapshot_logger_disabled() -> tuple[dict[str, bool], bool]:
    """Capture every existing logger's ``.disabled`` bit.

    Returned so :func:`_restore_logger_disabled` can undo
    ``logging.config.fileConfig(...)``'s
    ``disable_existing_loggers=True`` side effect once alembic
    finishes. The root logger has its own bit that isn't in
    ``loggerDict``, so it's tracked separately.
    """
    manager = logging.Logger.manager
    snap = {
        name: lg.disabled
        for name, lg in manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    return snap, logging.getLogger().disabled


def _restore_logger_disabled(snapshot: dict[str, bool], root_disabled: bool) -> None:
    """Reset each logger's ``.disabled`` bit to its captured value."""
    manager = logging.Logger.manager
    for name, was_disabled in snapshot.items():
        lg = manager.loggerDict.get(name)
        if isinstance(lg, logging.Logger):
            lg.disabled = was_disabled
    logging.getLogger().disabled = root_disabled


def run_upgrade_head_sync(database_url: str) -> None:
    """Run ``alembic upgrade head`` against ``database_url`` synchronously.

    Snapshots + restores ``KILN_DATABASE_URL`` and every logger's
    ``.disabled`` bit, so calling this from a long-running process
    (the API lifespan, the worker ``serve()``) doesn't leak alembic's
    fileConfig side effects into the rest of the process.

    Raises whatever ``alembic.command.upgrade`` raises — callers
    should let it propagate so the app crashes loudly rather than
    serving against an unmigrated DB.

    Safe to call from any thread; sync wrapper around the async
    machinery in ``alembic/env.py``.
    """
    # get_settings is cached; alembic/env.py reads it. Clear so the
    # injected DB URL is picked up.
    from cf_knowledge_kiln.config import get_settings

    saved_url = os.environ.get(_DB_URL_ENV)
    logger_snap, root_disabled = _snapshot_logger_disabled()
    os.environ[_DB_URL_ENV] = database_url
    get_settings.cache_clear()
    try:
        logger.info("alembic upgrade head: starting against %s", _redact(database_url))
        ini = _alembic_ini_path()
        cfg = Config(str(ini) if isinstance(ini, Path) else ini)
        command.upgrade(cfg, "head")
        logger.info("alembic upgrade head: done")
    finally:
        if saved_url is None:
            os.environ.pop(_DB_URL_ENV, None)
        else:
            os.environ[_DB_URL_ENV] = saved_url
        get_settings.cache_clear()
        _restore_logger_disabled(logger_snap, root_disabled)


async def run_upgrade_head(database_url: str) -> None:
    """Async wrapper — runs the sync helper off the event loop via to_thread.

    Required because ``alembic.command.upgrade`` calls
    ``asyncio.run(...)`` internally to drive the async-engine code
    path in ``alembic/env.py``. Calling ``asyncio.run`` from inside a
    running event loop raises ``RuntimeError``, so we hop to a worker
    thread (which runs its own loop for the inner ``asyncio.run``).
    """
    await asyncio.to_thread(run_upgrade_head_sync, database_url)


def _redact(database_url: str) -> str:
    """Strip the password from ``postgres://user:pwd@host/db`` for logs."""
    # Cheap, no parse — find ``://user:`` … ``@``.
    proto, _, rest = database_url.partition("://")
    if not rest or "@" not in rest:
        return database_url
    userinfo, _, hostpart = rest.partition("@")
    user, sep, _pwd = userinfo.partition(":")
    if not sep:
        return database_url  # no password to redact
    return f"{proto}://{user}:***@{hostpart}"


__all__ = ["run_upgrade_head", "run_upgrade_head_sync"]
