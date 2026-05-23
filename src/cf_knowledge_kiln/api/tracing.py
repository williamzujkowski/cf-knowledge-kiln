"""Optional OpenTelemetry tracer accessor (#178 Phase 2 follow-up).

The ``[otel]`` extra is optional — see ``api/observability.py``. Call
sites that want to emit custom spans (e.g. the retrieval engine's phase
boundaries) shouldn't have to branch on whether the extra is installed.
This module gives them a single ``get_tracer(name)`` entry point that
returns either the real OTel tracer or a no-op shim with the same
surface.

The shim is fully sufficient for our use: ``start_as_current_span(name,
attributes=...)`` returns a context manager yielding a span object with
``set_attribute`` / ``set_attributes`` / ``record_exception`` / ``add_event``.
None of those do anything when the shim is in play — but call sites stay
uncluttered.

When the extra IS installed AND ``configure_observability`` has wired a
TracerProvider, ``trace.get_tracer(name)`` returns a real tracer that
emits spans to the configured OTLP exporter. When the extra is installed
but no provider is configured, OTel itself returns a no-op tracer — so
calls remain cheap.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Protocol


class _SpanLike(Protocol):
    """Subset of the OTel Span surface that call sites use."""

    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_attributes(self, attributes: dict[str, Any]) -> None: ...
    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None: ...
    def record_exception(self, exception: BaseException) -> None: ...


class _NoOpSpan:
    """Drop-in no-op for an OTel Span. All methods are silent no-ops."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        return None

    def set_attributes(self, attributes: dict[str, Any]) -> None:  # noqa: ARG002
        return None

    def add_event(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> None:
        return None

    def record_exception(self, exception: BaseException) -> None:  # noqa: ARG002
        return None


class _NoOpTracer:
    """Drop-in no-op for an OTel Tracer.

    Implements only ``start_as_current_span`` — the one call site we use.
    Returns a context manager yielding a :class:`_NoOpSpan` so call sites
    can freely ``with tracer.start_as_current_span(...) as span:``.
    """

    @contextmanager
    def start_as_current_span(
        self,
        name: str,  # noqa: ARG002
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> Any:
        yield _NoOpSpan()


_NOOP_TRACER: _NoOpTracer = _NoOpTracer()


def get_tracer(name: str) -> Any:
    """Return an OTel-compatible tracer for ``name``.

    When the ``[otel]`` extra is installed: returns the real tracer
    from ``opentelemetry.trace``. When a TracerProvider has been
    wired (see ``api/observability.py``), spans flow to the configured
    exporter; otherwise OTel itself substitutes a no-op tracer.

    When the extra is NOT installed: returns a local no-op shim.
    Call sites stay uniform either way.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return _NOOP_TRACER
    return trace.get_tracer(name)


__all__ = ["get_tracer"]
