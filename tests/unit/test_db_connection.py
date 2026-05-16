"""Unit tests for the DB connection layer.

These tests exercise pure functions (VCAP_SERVICES parsing, URL
resolution). Live engine startup and ``/readyz`` integration are covered
separately.
"""

from __future__ import annotations

import json

import pytest

from cf_knowledge_kiln.config import Settings
from cf_knowledge_kiln.db.connection import parse_vcap_services, resolve_database_url


def test_parse_vcap_returns_none_when_env_missing() -> None:
    assert parse_vcap_services(None, "cf-knowledge-kiln-db") is None


def test_parse_vcap_returns_none_when_env_empty() -> None:
    assert parse_vcap_services("", "cf-knowledge-kiln-db") is None


def test_parse_vcap_returns_none_when_service_name_not_bound() -> None:
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "some-other-service",
                    "credentials": {"uri": "postgresql://u:p@h:5432/d"},  # pragma: allowlist secret
                }
            ]
        }
    )
    assert parse_vcap_services(vcap, "cf-knowledge-kiln-db") is None


def test_parse_vcap_extracts_uri_from_pgvector_binding() -> None:
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "label": "postgresql-local",
                    "plan": "pgvector",
                    "credentials": {
                        "uri": "postgresql://user:secret@host.example:5432/kiln",  # pragma: allowlist secret
                    },
                }
            ]
        }
    )
    result = parse_vcap_services(vcap, "cf-knowledge-kiln-db")
    expected = "postgresql+asyncpg://user:secret@host.example:5432/kiln"  # pragma: allowlist secret
    assert result == expected


def test_parse_vcap_rewrites_postgres_scheme_to_postgresql() -> None:
    """Some brokers emit the legacy `postgres://` scheme."""
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {"uri": "postgres://u:p@h:5432/d"},  # pragma: allowlist secret
                }
            ]
        }
    )
    result = parse_vcap_services(vcap, "cf-knowledge-kiln-db")
    assert result == "postgresql+asyncpg://u:p@h:5432/d"


def test_parse_vcap_passes_through_explicit_asyncpg_scheme() -> None:
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {
                        "uri": "postgresql+asyncpg://u:p@h:5432/d",  # pragma: allowlist secret
                    },
                }
            ]
        }
    )
    assert parse_vcap_services(vcap, "cf-knowledge-kiln-db") == "postgresql+asyncpg://u:p@h:5432/d"


def test_parse_vcap_assembles_dsn_from_parts_when_uri_missing() -> None:
    vcap = json.dumps(
        {
            "user-provided": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {
                        "host": "pg.example",
                        "port": 5432,
                        "username": "kiln",
                        "password": "secret",  # pragma: allowlist secret
                        "database": "kiln",
                    },
                }
            ]
        }
    )
    expected = "postgresql+asyncpg://kiln:secret@pg.example:5432/kiln"  # pragma: allowlist secret
    assert parse_vcap_services(vcap, "cf-knowledge-kiln-db") == expected


def test_parse_vcap_searches_across_labels() -> None:
    vcap = json.dumps(
        {
            "label-a": [{"name": "irrelevant", "credentials": {"uri": "postgresql://x"}}],
            "label-b": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {"uri": "postgresql://u:p@h:5432/d"},  # pragma: allowlist secret
                }
            ],
        }
    )
    assert parse_vcap_services(vcap, "cf-knowledge-kiln-db") == "postgresql+asyncpg://u:p@h:5432/d"


def test_parse_vcap_raises_when_credentials_missing_required_fields() -> None:
    vcap = json.dumps(
        {
            "user-provided": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {"host": "h"},  # no username/password/database/uri
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="cf-knowledge-kiln-db"):
        parse_vcap_services(vcap, "cf-knowledge-kiln-db")


def test_parse_vcap_raises_on_invalid_json() -> None:
    with pytest.raises(ValueError, match="VCAP_SERVICES"):
        parse_vcap_services("not-json", "cf-knowledge-kiln-db")


def test_parse_vcap_raises_when_top_level_not_object() -> None:
    with pytest.raises(ValueError, match="VCAP_SERVICES"):
        parse_vcap_services("[]", "cf-knowledge-kiln-db")


def test_resolve_url_prefers_explicit_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    s = Settings(database_url="postgresql+asyncpg://a:b@c:5432/d")  # pragma: allowlist secret
    assert resolve_database_url(s) == "postgresql+asyncpg://a:b@c:5432/d"


def test_resolve_url_normalizes_sync_scheme_in_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a user pastes a plain `postgresql://` URL into KILN_DATABASE_URL, normalize it."""
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    s = Settings(database_url="postgresql://a:b@c:5432/d")  # pragma: allowlist secret
    assert resolve_database_url(s) == "postgresql+asyncpg://a:b@c:5432/d"


def test_resolve_url_falls_back_to_vcap(monkeypatch: pytest.MonkeyPatch) -> None:
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "cf-knowledge-kiln-db",
                    "credentials": {"uri": "postgresql://u:p@h:5432/d"},  # pragma: allowlist secret
                }
            ]
        }
    )
    monkeypatch.setenv("VCAP_SERVICES", vcap)
    s = Settings(database_url=None, pg_service_name="cf-knowledge-kiln-db")
    assert resolve_database_url(s) == "postgresql+asyncpg://u:p@h:5432/d"


def test_resolve_url_returns_none_when_neither_source_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    s = Settings(database_url=None)
    assert resolve_database_url(s) is None


def test_resolve_url_respects_custom_pg_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    vcap = json.dumps(
        {
            "postgresql-local": [
                {
                    "name": "custom-binding-name",
                    "credentials": {"uri": "postgresql://u:p@h:5432/d"},  # pragma: allowlist secret
                }
            ]
        }
    )
    monkeypatch.setenv("VCAP_SERVICES", vcap)
    s = Settings(database_url=None, pg_service_name="custom-binding-name")
    assert resolve_database_url(s) == "postgresql+asyncpg://u:p@h:5432/d"
