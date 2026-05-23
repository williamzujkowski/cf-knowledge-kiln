"""OpenTelemetry tracing wiring (#178 follow-up).

The ``KILN_OTEL_*`` settings used to be accepted-but-unwired. This
module makes them do something: when ``KILN_OTEL_EXPORTER_OTLP_ENDPOINT``
is set, the lifespan configures an OTLP-HTTP exporter + FastAPI
auto-instrumentation. When the env var is unset, the function is a
silent no-op — production deploys that don't want tracing just leave
it unset.

The OpenTelemetry packages are an **optional** extra (``[otel]``):

    pip install -e '.[otel]'

If the endpoint is set but the extra is not installed, this logs a
warning and continues without tracing — the app still starts. That
keeps a forgotten ``pip install`` from crash-looping a deploy.

Phase 1 (this file) installs FastAPI auto-instrumentation only: every
HTTP request emits a span. Custom retrieval-phase spans (embed, FTS,
vector, RRF, context-pack assembly) are a follow-up.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from cf_knowledge_kiln.config import Settings

logger = logging.getLogger(__name__)


def configure_observability(app: FastAPI, settings: Settings) -> None:
    """Configure OTel tracing if the env var is set and the extra is installed.

    Idempotent on the no-op path; safe to call unconditionally from
    ``create_app``.
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning(
            "KILN_OTEL_EXPORTER_OTLP_ENDPOINT is set but the [otel] extra "
            "is not installed (%s); tracing disabled. Install with: "
            "pip install -e '.[otel]'",
            exc,
        )
        return
    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    logger.info(
        "OpenTelemetry tracing configured: service=%s endpoint=%s",
        settings.otel_service_name,
        endpoint,
    )
