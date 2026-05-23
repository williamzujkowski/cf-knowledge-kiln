"""Tests for the OpenTelemetry tracing wiring (#178 follow-up).

`configure_observability` has three branches:

* endpoint unset → no-op
* endpoint set + [otel] extra missing → warn + no-op (app still starts)
* endpoint set + [otel] extra installed → tracer provider configured +
  FastAPI auto-instrumented

The "extra installed" case is skipped when the optional dep is not on
sys.path (CI's default install). The first two branches always run.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from unittest.mock import MagicMock

import pytest

from cf_knowledge_kiln.api.observability import configure_observability
from cf_knowledge_kiln.config import Settings


def _otel_extra_installed() -> bool:
    """True iff every submodule `configure_observability` imports is present."""
    required = (
        "opentelemetry",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.resources",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.instrumentation.fastapi",
    )
    return all(importlib.util.find_spec(m) is not None for m in required)


_OTEL_AVAILABLE = _otel_extra_installed()


def _settings(*, endpoint: str | None) -> Settings:
    return Settings(
        _env_file=None,
        otel_exporter_otlp_endpoint=endpoint,
    )  # type: ignore[call-arg]


def test_unset_endpoint_is_a_noop(caplog: pytest.LogCaptureFixture) -> None:
    """No endpoint = no configuration, no warning, no error."""
    app = MagicMock()  # configure_observability returns early; app is untouched
    with caplog.at_level(logging.DEBUG, logger="cf_knowledge_kiln.api.observability"):
        configure_observability(app, _settings(endpoint=None))
    assert caplog.records == []


def test_endpoint_set_without_extra_warns_and_continues(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[otel] extra missing → log a warning, do NOT raise.

    Simulated by stubbing the opentelemetry package out of sys.modules so
    the inner `import opentelemetry...` raises ImportError. This is the
    deploy-forgot-the-extra path; the app must keep starting.
    """
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    app = MagicMock()
    with caplog.at_level(logging.WARNING, logger="cf_knowledge_kiln.api.observability"):
        configure_observability(app, _settings(endpoint="http://localhost:4318/v1/traces"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning about the missing [otel] extra"
    assert "otel" in warnings[0].getMessage().lower()


@pytest.mark.skipif(not _OTEL_AVAILABLE, reason="[otel] extra not installed")
def test_endpoint_set_with_extra_configures_tracer(caplog: pytest.LogCaptureFixture) -> None:
    """With the [otel] extra installed, a tracer provider is configured."""
    from fastapi import FastAPI
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    app = FastAPI()
    with caplog.at_level(logging.INFO, logger="cf_knowledge_kiln.api.observability"):
        configure_observability(app, _settings(endpoint="http://localhost:4318/v1/traces"))
    assert isinstance(trace.get_tracer_provider(), TracerProvider)
    assert any("OpenTelemetry tracing configured" in r.getMessage() for r in caplog.records)
