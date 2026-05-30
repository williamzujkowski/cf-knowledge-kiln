"""#333 unit tests for the HyDE settings.

Validates env-var precedence + default values + bounds. The actual
HyDE wiring happens in app.lifespan + dependencies — covered by
``tests/integration/test_hybrid_retrieval_hyde.py``.
"""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.config.settings import Settings


class TestHydeDefaults:
    def test_disabled_by_default(self) -> None:
        s = Settings()
        assert s.hyde_enabled is False

    def test_token_threshold_default(self) -> None:
        assert Settings().hyde_query_token_threshold == 8

    def test_jargon_density_threshold_default(self) -> None:
        assert Settings().hyde_jargon_density_threshold == 0.4

    def test_cache_max_entries_default(self) -> None:
        assert Settings().hyde_cache_max_entries == 1000

    def test_cache_ttl_default(self) -> None:
        # 86400s = 24h — long enough that frequent queries hit the cache
        # across a working day; short enough that a model swap eventually
        # purges stale entries even without an explicit flush.
        assert Settings().hyde_cache_ttl_seconds == 86400

    def test_generator_max_tokens_default(self) -> None:
        assert Settings().hyde_generator_max_tokens == 200

    def test_generator_timeout_default(self) -> None:
        assert Settings().hyde_generator_timeout_seconds == 3.0


class TestHydeEnvOverrides:
    """Env vars with the ``KILN_`` prefix override defaults."""

    def test_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_ENABLED", "true")
        assert Settings().hyde_enabled is True

    def test_token_threshold_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_QUERY_TOKEN_THRESHOLD", "12")
        assert Settings().hyde_query_token_threshold == 12

    def test_jargon_density_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_JARGON_DENSITY_THRESHOLD", "0.6")
        assert Settings().hyde_jargon_density_threshold == pytest.approx(0.6)

    def test_cache_max_entries_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_CACHE_MAX_ENTRIES", "2500")
        assert Settings().hyde_cache_max_entries == 2500

    def test_cache_ttl_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_CACHE_TTL_SECONDS", "3600")
        assert Settings().hyde_cache_ttl_seconds == 3600

    def test_generator_max_tokens_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_GENERATOR_MAX_TOKENS", "300")
        assert Settings().hyde_generator_max_tokens == 300

    def test_generator_timeout_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_GENERATOR_TIMEOUT_SECONDS", "5.0")
        assert Settings().hyde_generator_timeout_seconds == pytest.approx(5.0)


class TestHydeTypeCoercion:
    """Env-var values arrive as strings; pydantic-settings coerces."""

    def test_enabled_accepts_string_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_ENABLED", "false")
        assert Settings().hyde_enabled is False

    def test_enabled_accepts_string_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_ENABLED", "1")
        assert Settings().hyde_enabled is True

    def test_enabled_accepts_string_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KILN_HYDE_ENABLED", "0")
        assert Settings().hyde_enabled is False
