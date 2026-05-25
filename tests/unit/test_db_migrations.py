"""Unit tests for :mod:`cf_knowledge_kiln.db.migrations` (#244).

These cover the side-effect isolation contract (env var + logger
disabled-state snapshot/restore) without touching a real database.
The integration test in ``tests/integration/test_auto_migrate.py``
exercises the actual ``alembic upgrade head`` path end-to-end.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import patch

import pytest

from cf_knowledge_kiln.db.migrations import (
    _redact,
    run_upgrade_head,
    run_upgrade_head_sync,
)


class TestRunUpgradeHeadSync:
    """Snapshot/restore contract for the sync helper."""

    def test_restores_database_url_env_var_after_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test sentinel env value must survive the helper's swap."""
        sentinel = "postgresql+asyncpg://sentinel@orig/db"  # pragma: allowlist secret
        monkeypatch.setenv("KILN_DATABASE_URL", sentinel)
        with patch("cf_knowledge_kiln.db.migrations.command.upgrade") as up:
            up.return_value = None
            run_upgrade_head_sync(
                "postgresql+asyncpg://other@target/db"
            )  # pragma: allowlist secret
        assert os.environ.get("KILN_DATABASE_URL") == sentinel

    def test_clears_database_url_env_var_if_originally_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the caller's env had no DB URL, helper must leave it that way."""
        monkeypatch.delenv("KILN_DATABASE_URL", raising=False)
        with patch("cf_knowledge_kiln.db.migrations.command.upgrade") as up:
            up.return_value = None
            run_upgrade_head_sync("postgresql+asyncpg://t/d")  # pragma: allowlist secret
        assert "KILN_DATABASE_URL" not in os.environ

    def test_restores_database_url_even_if_alembic_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = "postgresql+asyncpg://sentinel@orig/db"  # pragma: allowlist secret
        monkeypatch.setenv("KILN_DATABASE_URL", sentinel)
        with (
            patch("cf_knowledge_kiln.db.migrations.command.upgrade") as up,
            pytest.raises(RuntimeError, match="boom"),
        ):
            up.side_effect = RuntimeError("boom")
            run_upgrade_head_sync(
                "postgresql+asyncpg://other@target/db"
            )  # pragma: allowlist secret
        # finally-block must still restore even on raise.
        assert os.environ.get("KILN_DATABASE_URL") == sentinel

    def test_restores_logger_disabled_state(self) -> None:
        """Simulate ``logging.config.fileConfig(disable_existing_loggers=True)``
        flipping a logger's bit during the upgrade — helper must restore."""
        target_logger = logging.getLogger("cf_knowledge_kiln.test.victim")
        original = target_logger.disabled

        def upgrade_disables_logger(_cfg: Any, _rev: str) -> None:
            target_logger.disabled = True

        with patch(
            "cf_knowledge_kiln.db.migrations.command.upgrade",
            side_effect=upgrade_disables_logger,
        ):
            run_upgrade_head_sync("postgresql+asyncpg://t/d")  # pragma: allowlist secret
        assert target_logger.disabled == original

    def test_propagates_alembic_errors(self) -> None:
        """Migration failures must crash callers, not be swallowed."""
        with (
            patch(
                "cf_knowledge_kiln.db.migrations.command.upgrade",
                side_effect=RuntimeError("migration failed"),
            ),
            pytest.raises(RuntimeError, match="migration failed"),
        ):
            run_upgrade_head_sync("postgresql+asyncpg://t/d")  # pragma: allowlist secret

    def test_passes_database_url_to_alembic_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """During the upgrade call, KILN_DATABASE_URL must be set to
        the argument so alembic/env.py picks up the right DSN."""
        observed: dict[str, str] = {}

        def upgrade_captures_env(_cfg: Any, _rev: str) -> None:
            observed["url"] = os.environ.get("KILN_DATABASE_URL", "<unset>")

        monkeypatch.delenv("KILN_DATABASE_URL", raising=False)
        with patch(
            "cf_knowledge_kiln.db.migrations.command.upgrade",
            side_effect=upgrade_captures_env,
        ):
            run_upgrade_head_sync("postgresql+asyncpg://target@host/db")  # pragma: allowlist secret
        assert observed["url"] == "postgresql+asyncpg://target@host/db"


class TestRunUpgradeHeadAsync:
    async def test_runs_sync_helper_in_a_worker_thread(self) -> None:
        """The async wrapper hops to a thread so the inner asyncio.run
        from alembic/env.py doesn't trip the nested-loop guard."""
        calls: list[str] = []

        def fake_sync(url: str) -> None:
            calls.append(url)

        with patch(
            "cf_knowledge_kiln.db.migrations.run_upgrade_head_sync",
            side_effect=fake_sync,
        ):
            await run_upgrade_head("postgresql+asyncpg://target/db")  # pragma: allowlist secret
        assert calls == ["postgresql+asyncpg://target/db"]  # pragma: allowlist secret


class TestAlembicIniPathResolution:
    """The path resolver must work in both editable + wheel-installed layouts (#244)."""

    def test_returns_path_object_when_file_relative_exists(self) -> None:
        """Source-tree / editable-install path: returns the absolute Path."""
        from cf_knowledge_kiln.db.migrations import _alembic_ini_path

        out = _alembic_ini_path()
        # In the test env we ARE in the source tree, so the file
        # exists at the file-relative location.
        assert isinstance(out, type(out))  # at minimum a Path-like
        from pathlib import Path as _P

        assert isinstance(out, _P)
        assert out.exists()
        assert out.name == "alembic.ini"

    def test_falls_back_to_cwd_relative_string_when_file_relative_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wheel-installed layout (#244): when the file-relative
        ``alembic.ini`` doesn't exist (the kiln is installed
        non-editably and ``__file__`` lives in site-packages), the
        helper returns the literal string so Alembic resolves from
        CWD."""
        from pathlib import Path as _P

        from cf_knowledge_kiln.db import migrations as mig

        # Point the resolver at a directory we know has no alembic.ini.
        fake_file = _P("/nonexistent/site-packages/cf_knowledge_kiln/db/migrations.py")
        monkeypatch.setattr(mig, "__file__", str(fake_file))
        out = mig._alembic_ini_path()
        assert out == "alembic.ini"


class TestRedact:
    """Password redaction for log lines."""

    def test_strips_password_from_user_password_form(self) -> None:
        out = _redact("postgresql+asyncpg://kiln:secret@db/main")  # pragma: allowlist secret
        assert "secret" not in out
        assert "kiln" in out  # user retained
        assert "***" in out

    def test_passthrough_when_no_password(self) -> None:
        url = "postgresql+asyncpg://kiln@db/main"
        assert _redact(url) == url

    def test_passthrough_when_no_at_sign(self) -> None:
        url = "sqlite:///tmp/test.db"
        assert _redact(url) == url
