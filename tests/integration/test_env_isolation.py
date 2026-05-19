"""Regression guard for #154: session-scoped fixtures must not leak env state.

The ``_apply_migrations`` fixture in ``tests/integration/conftest.py`` sets
``KILN_DATABASE_URL`` so Alembic's env.py can resolve the DSN, and it loads
Alembic's logging config (which disables pre-existing loggers). Both side
effects must be confined to the fixture's setup phase — unit tests
interleaved later in the same pytest run must observe the original
environment and the original logger states.

Pre-fix: ``pytest tests/unit/ tests/eval/`` reported failures that
disappeared when each tier ran in isolation, because the eval-tier
session fixture left ``KILN_DATABASE_URL`` set for the rest of the
session and silently disabled the project's package loggers.

These tests exercise the migration helper directly (in-process) so we
can observe the post-teardown state without relying on pytest collection
order. The helper is called via the same code path as the fixture, just
without the autouse machinery.
"""

from __future__ import annotations

import logging
import os

import pytest

from tests.integration._migration_isolation import apply_migrations_with_isolation

pytestmark = pytest.mark.integration

_SENTINEL = object()


def test_apply_migrations_helper_restores_database_url(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration helper must not leak ``KILN_DATABASE_URL`` past upgrade.

    The helper sets the var during ``command.upgrade`` so alembic's
    env.py can resolve the DSN, but unwinds the assignment BEFORE
    yielding — otherwise any test collected later in the same pytest
    session would observe the test DSN through ``get_settings()``.

    This is the assertion that catches the #154 regression: prior to
    the fix the helper set the var inside an ``os.environ[...] = ...``
    assignment, yielded with the var still set, and only restored on
    the session-teardown ``finally``. Unit tests interleaved by pytest
    collection saw the test DSN through ``get_settings()`` because
    the yield held open for the entire session.

    The test forcibly unsets the env var first (via ``monkeypatch``,
    which auto-restores after the test) so the assertion has a clean
    pre-condition independent of how the session-scoped autouse
    fixture left the environment.
    """
    monkeypatch.delenv("KILN_DATABASE_URL", raising=False)
    with apply_migrations_with_isolation(database_url):
        # By the time the context yields the helper has already
        # restored the env var to its pre-call value (unset). Pre-fix
        # this assertion would see ``database_url`` (the test DSN)
        # because the helper yielded with the env var still set.
        inside = os.environ.get("KILN_DATABASE_URL", _SENTINEL)
        assert inside is _SENTINEL, (
            f"helper still has KILN_DATABASE_URL={inside!r} set inside the "
            "yield; every test collected after the upgrade would inherit it"
        )
    after = os.environ.get("KILN_DATABASE_URL", _SENTINEL)
    assert after is _SENTINEL, (
        f"migration helper leaked KILN_DATABASE_URL past context exit: {after!r}"
    )


def test_apply_migrations_helper_restores_logger_state(database_url: str) -> None:
    """Alembic's ``fileConfig`` call must not silence project loggers.

    ``logging.config.fileConfig`` defaults to ``disable_existing_loggers=True``,
    which sets ``.disabled = True`` on every logger not named in
    alembic.ini. That includes the entire ``cf_knowledge_kiln.*`` tree,
    so a unit test using ``caplog`` to assert a WARNING emission would
    silently see zero records once migrations had run.
    """
    target = logging.getLogger("cf_knowledge_kiln.ingestion.prompt_injection")
    pre = target.disabled
    with apply_migrations_with_isolation(database_url):
        pass
    assert target.disabled == pre, (
        f"logger {target.name!r} disabled state changed from {pre} to "
        f"{target.disabled} across the migration helper"
    )
