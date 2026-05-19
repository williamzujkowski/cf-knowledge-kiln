"""Settings loading and overrides."""

from __future__ import annotations

import pytest

from cf_knowledge_kiln.config import Settings, get_settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.app_name == "cf-knowledge-kiln"
    assert settings.env == "development"
    assert settings.log_level == "INFO"
    assert settings.http_port == 8080
    assert settings.auth_mode == "none"
    assert settings.default_max_chunks == 8
    assert settings.default_max_tokens == 3000
    assert settings.hnsw_ef_search == 200


def test_hnsw_ef_search_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_HNSW_EF_SEARCH", "300")
    assert Settings().hnsw_ef_search == 300


def test_ingest_embed_fanout_defaults() -> None:
    """PR C: batch/concurrency defaults sized for CPU-backed providers."""
    settings = Settings()
    assert settings.ingest_embed_batch_size == 32
    assert settings.ingest_embed_concurrency == 4


def test_ingest_embed_fanout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_INGEST_EMBED_BATCH_SIZE", "16")
    monkeypatch.setenv("KILN_INGEST_EMBED_CONCURRENCY", "8")
    settings = Settings()
    assert settings.ingest_embed_batch_size == 16
    assert settings.ingest_embed_concurrency == 8


def test_status_preference_list_parses_csv() -> None:
    settings = Settings(default_status_preference="active, approved , draft")
    assert settings.status_preference_list == ["active", "approved", "draft"]


def test_status_preference_list_strips_empties() -> None:
    settings = Settings(default_status_preference="active,,approved,")
    assert settings.status_preference_list == ["active", "approved"]


def test_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_HTTP_PORT", "9090")
    monkeypatch.setenv("KILN_ENV", "production")
    monkeypatch.setenv("KILN_LOG_LEVEL", "WARNING")
    settings = Settings()
    assert settings.http_port == 9090
    assert settings.env == "production"
    assert settings.log_level == "WARNING"


def test_get_settings_is_cached() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_invalid_env_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(env="banana")  # type: ignore[arg-type]


def test_invalid_auth_mode_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(auth_mode="oauth")  # type: ignore[arg-type]
