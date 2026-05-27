"""Pins the #359 GET /v1/registry surface.

* The repository aggregates dimensions correctly + drops null/empty.
* The cache wraps the repo with a TTL.
* The Pydantic models round-trip through OpenAPI without drift.

DB-touching integration coverage lives in tests/integration/; the
unit tests here use stub sessions + monkeypatched repositories so
they run in the fast tier.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from cf_knowledge_kiln.retrieval.types import (
    RegistryDimension,
    RegistryResponse,
    RegistryValue,
)


class TestRegistryDimensionEnum:
    def test_seven_dimensions_documented(self) -> None:
        """Closed set per the OpenAPI schema. Adding a dimension is a
        deliberate API change — pin the count so a silent expansion
        gets caught."""
        from typing import get_args

        assert set(get_args(RegistryDimension)) == {
            "status",
            "doc_type",
            "owner",
            "repo",
            "authority",
            "sensitivity",
            "system",
        }


class TestRegistryResponseShape:
    def test_round_trip_pydantic(self) -> None:
        resp = RegistryResponse(
            dimensions={
                "doc_type": [
                    RegistryValue(value="runbook", count=12, last_indexed=date(2026, 5, 1)),
                    RegistryValue(value="adr", count=4, last_indexed=None),
                ],
            },
            as_of=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        )
        # The wire shape MUST serialize cleanly + round-trip via
        # model_validate so the OpenAPI drift test stays happy.
        dumped = resp.model_dump(mode="json", exclude_none=True)
        rehydrated = RegistryResponse.model_validate(dumped)
        assert rehydrated == resp

    def test_last_indexed_optional(self) -> None:
        """No date in the bucket is a legal state (e.g. a doc_type
        whose docs all lack last_reviewed). The OpenAPI shows it
        as nullable; the Pydantic model has a default of None."""
        v = RegistryValue(value="runbook", count=3)
        assert v.last_indexed is None

    def test_count_must_be_non_negative(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RegistryValue(value="runbook", count=-1)


class TestRegistryCache:
    """The route caches the aggregated response process-locally for
    the configured TTL. Pin both ends: the first call populates,
    subsequent calls within the TTL return the cached payload, and
    a call after the TTL expires re-aggregates."""

    @pytest.mark.asyncio
    async def test_cache_populated_on_first_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cf_knowledge_kiln.api import registry as mod

        mod._reset_cache_for_tests()
        calls: list[int] = []

        async def _fake_build(_session: Any) -> RegistryResponse:
            calls.append(1)
            return RegistryResponse(
                dimensions={"status": [RegistryValue(value="active", count=1)]},
                as_of=datetime.now(UTC),
            )

        monkeypatch.setattr(mod, "_build_response", _fake_build)
        # First call → builds.
        resp = await mod.registry(session=object(), dimension=None)  # type: ignore[arg-type]
        assert len(calls) == 1
        assert resp.dimensions["status"][0].value == "active"
        # Second call → cached.
        resp2 = await mod.registry(session=object(), dimension=None)  # type: ignore[arg-type]
        assert len(calls) == 1  # NOT incremented
        assert resp2 == resp

    @pytest.mark.asyncio
    async def test_expired_cache_rebuilds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cf_knowledge_kiln.api import registry as mod

        mod._reset_cache_for_tests()
        calls: list[int] = []

        async def _fake_build(_session: Any) -> RegistryResponse:
            calls.append(1)
            return RegistryResponse(
                dimensions={"status": [RegistryValue(value="active", count=1)]},
                as_of=datetime.now(UTC),
            )

        monkeypatch.setattr(mod, "_build_response", _fake_build)
        await mod.registry(session=object(), dimension=None)  # type: ignore[arg-type]
        assert len(calls) == 1

        # Force expiry: rewind the cache timestamp past the TTL.
        from cf_knowledge_kiln.config import get_settings

        mod._cache_built_at = datetime.now(UTC) - timedelta(
            seconds=get_settings().registry_cache_seconds + 1
        )
        await mod.registry(session=object(), dimension=None)  # type: ignore[arg-type]
        # Rebuilt → call count incremented.
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_dimension_filter_returns_single_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cf_knowledge_kiln.api import registry as mod

        mod._reset_cache_for_tests()

        async def _fake_build(_session: Any) -> RegistryResponse:
            return RegistryResponse(
                dimensions={
                    "status": [RegistryValue(value="active", count=10)],
                    "doc_type": [RegistryValue(value="runbook", count=5)],
                    "owner": [],
                    "repo": [],
                    "authority": [],
                    "sensitivity": [],
                    "system": [],
                },
                as_of=datetime.now(UTC),
            )

        monkeypatch.setattr(mod, "_build_response", _fake_build)
        resp = await mod.registry(session=object(), dimension="doc_type")  # type: ignore[arg-type]
        # Only the requested dimension key is present.
        assert set(resp.dimensions) == {"doc_type"}
        assert resp.dimensions["doc_type"][0].value == "runbook"


class TestRegistryRepositoryShape:
    """The repository declares a closed set of dimensions; aggregate
    queries hit a known column per dimension; NULL + empty-string
    values are dropped."""

    def test_supported_dimensions_match_enum(self) -> None:
        from typing import get_args

        from cf_knowledge_kiln.db.repositories import RegistryRepository

        # Build a no-op repo (no session needed for supported_dimensions).
        repo = RegistryRepository(session=None)  # type: ignore[arg-type]
        assert set(repo.supported_dimensions()) == set(get_args(RegistryDimension))

    def test_aggregate_unknown_dimension_raises(self) -> None:
        import asyncio

        from cf_knowledge_kiln.db.repositories import RegistryRepository

        repo = RegistryRepository(session=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown registry dimension"):
            asyncio.run(repo.aggregate(dimension="not_a_real_dim"))


class TestSettingsKnob:
    def test_registry_cache_seconds_defaults_to_300(self) -> None:
        from cf_knowledge_kiln.config import Settings

        s = Settings()
        assert s.registry_cache_seconds == 300


class TestOpenAPISchemaPinned:
    """The drift test in test_openapi_drift.py covers field-name
    parity. This file adds explicit content pins so a future
    refactor can't silently drop the new route."""

    def test_path_present(self) -> None:
        from pathlib import Path

        yaml = Path(__file__).resolve().parents[2] / "openapi/openapi.yaml"
        text = yaml.read_text()
        assert "/v1/registry:" in text
        assert "RegistryDimension:" in text
        assert "RegistryResponse:" in text
        assert "RegistryValue:" in text
