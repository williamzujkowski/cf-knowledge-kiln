"""Reusable context manager that scopes Alembic-migration side effects.

This lives in a non-conftest module so the regression guard at
``tests/integration/test_env_isolation.py`` can import it directly via
``tests.integration._migration_isolation`` without colliding with the
``pytest_plugins = ["tests.integration.conftest"]`` directive in
``tests/eval/conftest.py``. Importing the conftest under its dotted
name would double-register it (once as a file-path plugin, once as a
named plugin) and fail collection.

The autouse session fixture in :mod:`tests.integration.conftest` uses
this helper too; everything that needs the isolation goes through
:func:`apply_migrations_with_isolation`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from alembic import command
from alembic.config import Config

from cf_knowledge_kiln.config import get_settings


def _snapshot_logger_disabled() -> tuple[dict[str, bool], bool]:
    """Capture every existing logger's ``.disabled`` bit.

    Returned so :func:`_restore_logger_disabled` can undo
    ``logging.config.fileConfig(...)``'s ``disable_existing_loggers=True``
    side effect once alembic finishes. The root logger has its own bit
    that isn't in ``loggerDict``, so it's tracked separately.
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


@contextmanager
def apply_migrations_with_isolation(database_url: str) -> Iterator[None]:
    """Run Alembic ``upgrade head`` with full env + logging isolation (#154).

    Two side effects of ``alembic.command.upgrade`` leak across the
    session boundary if left unmanaged:

    1. **``KILN_DATABASE_URL``**: alembic/env.py reads the DSN via
       ``resolve_database_url(get_settings())``, so the var must be
       set during the upgrade. A direct ``os.environ[...] = ...``
       assignment escapes the fixture's lifetime and leaks the test
       DSN into unit tests that read settings later in the same
       pytest run.
    2. **Logger ``disabled`` state**: alembic/env.py calls
       ``logging.config.fileConfig(...)`` with the default
       ``disable_existing_loggers=True``, which marks every logger
       not named in ``alembic.ini`` as disabled. That silences the
       entire ``cf_knowledge_kiln.*`` tree — caplog-based unit tests
       run later in the same session would see zero records.

    Both effects are captured and unwound BEFORE the context yields,
    so subsequent tests in the same session observe the original
    ambient state — even integration tests, which set
    ``KILN_DATABASE_URL`` themselves per-test via the function-scoped
    fixtures in test modules.

    Migrations themselves are session-scoped — they only need to run
    once. The env var and logger state are *narrowly* scoped to that
    single ``command.upgrade`` call rather than to the whole session.
    """
    saved_url: str | None = os.environ.get("KILN_DATABASE_URL")
    logger_snap, root_disabled = _snapshot_logger_disabled()
    os.environ["KILN_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
    finally:
        # Unwind BEFORE yield so the rest of the session sees the
        # original ambient state. The migration has already committed
        # via Alembic's own connection; tests can reach the DB via
        # the ``database_url`` fixture without needing the env var.
        if saved_url is None:
            os.environ.pop("KILN_DATABASE_URL", None)
        else:
            os.environ["KILN_DATABASE_URL"] = saved_url
        get_settings.cache_clear()
        _restore_logger_disabled(logger_snap, root_disabled)
    yield


__all__ = ["apply_migrations_with_isolation"]
