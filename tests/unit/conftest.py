"""Unit-test fixtures + ambient setup.

Unit tests run without a real database (that's what makes them
unit tests). Production defaults that touch the DB at startup get
opted-out here so an individual test doesn't have to.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.config import get_settings


@pytest.fixture(autouse=True)
def _disable_auto_migrate_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """#244: production defaults ON; unit tests opt OUT.

    Lifespan / API construction tests set a fake KILN_DATABASE_URL but
    don't reach a real Postgres — the auto-migrate path would error
    out trying to connect. Force-disable so the lifespan skips the
    upgrade in unit-test scope. Integration tests that exercise the
    real flow set the env var explicitly.
    """
    monkeypatch.setenv("KILN_AUTO_MIGRATE_ON_STARTUP", "false")
    get_settings.cache_clear()
