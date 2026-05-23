"""Tests for the optional-OTel tracer accessor (#178 Phase 2).

`api/tracing.get_tracer(name)` must work in three modes:

* `[otel]` extra not installed → no-op shim (silent, no exceptions)
* `[otel]` extra installed, no TracerProvider wired → OTel's own NoOp tracer
* `[otel]` extra installed, TracerProvider wired → real tracer emitting spans

The first mode always runs in CI (no `[otel]` by default). The
provider-wired case is covered indirectly by `test_observability.py`.
This module focuses on the surface call sites depend on: span context
manager + attribute setters never raise, regardless of mode.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from cf_knowledge_kiln.api.tracing import get_tracer

_OTEL_INSTALLED = importlib.util.find_spec("opentelemetry") is not None


def test_get_tracer_returns_object_with_start_as_current_span() -> None:
    tracer = get_tracer(__name__)
    assert hasattr(tracer, "start_as_current_span")


def test_span_context_manager_yields_attribute_setter_surface() -> None:
    """Whether shim or real tracer, the yielded span supports our calls."""
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("test.span") as span:
        span.set_attribute("key", "value")
        span.set_attribute("count", 7)
        span.set_attributes({"a": 1, "b": "two"})
        span.add_event("event-name", attributes={"k": "v"})
        # set_attribute on an int value is allowed by OTel.


def test_span_attributes_kwarg_accepted_at_start() -> None:
    """The ``attributes=`` constructor kwarg must not raise either."""
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("test.span", attributes={"retrieval.consumer_type": "test"}):
        pass


def test_span_records_exception_without_swallowing() -> None:
    """`record_exception` must accept any BaseException without raising itself.

    The shim records nothing; the real tracer attaches it to the span.
    In both cases the *caller's* exception must continue to propagate
    out of the `with` block.
    """
    tracer = get_tracer(__name__)
    with (
        pytest.raises(RuntimeError, match="boom"),
        tracer.start_as_current_span("test.span") as span,
    ):
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            span.record_exception(exc)
            raise


def test_no_op_shim_returned_when_otel_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forcing `opentelemetry` out of sys.modules selects the shim path."""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    tracer = get_tracer(__name__)
    # Shim is a private class; identify it by class name to avoid
    # importing the private symbol.
    assert type(tracer).__name__ == "_NoOpTracer"
    with tracer.start_as_current_span("x") as span:
        assert type(span).__name__ == "_NoOpSpan"


@pytest.mark.skipif(not _OTEL_INSTALLED, reason="[otel] extra not installed")
def test_real_tracer_used_when_otel_present() -> None:
    """With the [otel] extra installed, get_tracer returns a real OTel tracer."""
    from opentelemetry.trace import Tracer

    tracer = get_tracer(__name__)
    assert isinstance(tracer, Tracer)
