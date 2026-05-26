# Observability

The kiln emits OpenTelemetry traces when an OTLP-HTTP collector
endpoint is configured AND the optional `[otel]` extra is installed.
Tracing is off by default — call sites are no-ops, no spans are
created, no overhead is paid.

> **For per-request audit-trail reconstruction** (tracing a
> `request_id` / `answer_id` / `context_pack_id` from a user
> complaint to the chunks the agent saw), see the dedicated
> [runbooks/audit-trail.md](./runbooks/audit-trail.md). This page
> covers the OTel trace surface; that runbook covers the SQL +
> `cf logs` recipe for the operator-on-call.

## Turning tracing on

Two things must be true together:

1. `pip install -e '.[otel]'` (or include the extra in your deployment
   image). This brings in `opentelemetry-api`, `-sdk`,
   `-instrumentation-fastapi`, and the OTLP-HTTP exporter.
2. Set `KILN_OTEL_EXPORTER_OTLP_ENDPOINT=<url>` to your collector. The
   `KILN_OTEL_SERVICE_NAME` setting (default `cf-knowledge-kiln`) is
   reported as `service.name` on every span.

Either condition missing → tracing stays off and the app starts
normally. A configured endpoint without the extra logs a single
WARNING at startup and continues; see
`src/cf_knowledge_kiln/api/observability.py`.

## What gets traced

### HTTP layer (Phase 1, PR #193)

`FastAPIInstrumentor.instrument_app(app)` wraps the ASGI app, so every
request gets a root server span carrying method, route, status code,
and duration. This is the OTel default and matches what other
FastAPI-instrumented services emit, so you can correlate with
upstream/downstream traces.

### Retrieval layer (Phase 2, PR #196)

Inside a `/v1/search` or `/v1/agent/context-pack` request, the
retrieval engine opens named child spans for each phase so traces
show where time goes inside the request:

```text
HTTP server span (FastAPIInstrumentor)
└─ retrieval.search                   (human path)
   ├─ retrieval.normalize_query
   ├─ retrieval.embed_query           (only if an embedding provider is configured)
   ├─ retrieval.sql.hybrid_search     OR retrieval.sql.fts_search
   ├─ retrieval.apply_boosts
   └─ retrieval.collect_warnings

HTTP server span
└─ retrieval.context_pack             (agent path — superset of search)
   ├─ retrieval.normalize_query
   ├─ retrieval.embed_query
   ├─ retrieval.sql.hybrid_search     OR retrieval.sql.fts_search
   ├─ retrieval.apply_boosts
   ├─ retrieval.collect_warnings
   ├─ retrieval.detect_conflicts
   └─ retrieval.assemble_context_pack
```

## Span attribute vocabulary

All retrieval-phase attributes are namespaced under `retrieval.*` so
they don't collide with HTTP / OTel-semantic-convention attributes.

| Attribute                                  | Type   | Where it's set                       |
| ------------------------------------------ | ------ | ------------------------------------ |
| `retrieval.consumer_type`                  | string | Root span (`human` or `agent`)       |
| `retrieval.query_length`                   | int    | Root span                            |
| `retrieval.max_results`                    | int    | `retrieval.search` root              |
| `retrieval.max_chunks`                     | int    | `retrieval.context_pack` root        |
| `retrieval.max_tokens`                     | int    | `retrieval.context_pack` root        |
| `retrieval.chunks_returned`                | int    | Root span (post-trim)                |
| `retrieval.warnings_count`                 | int    | Root span + `collect_warnings` span  |
| `retrieval.conflicts_count`                | int    | `context_pack` root + `detect_conflicts` |
| `retrieval.removed_phrases_count`          | int    | `normalize_query`                    |
| `retrieval.embedding.provider`             | string | `embed_query`                        |
| `retrieval.embedding.model`                | string | `embed_query`                        |
| `retrieval.embedding.dimensions`           | int    | `embed_query`                        |
| `retrieval.ef_search`                      | int    | `sql.hybrid_search`                  |
| `retrieval.rows_returned`                  | int    | `sql.hybrid_search` + `sql.fts_search` |
| `retrieval.chunks_in` / `.chunks_kept`     | int    | `apply_boosts`                       |
| `retrieval.tokens_used_estimate`           | int    | `assemble_context_pack`              |
| `retrieval.requires_human_review`          | bool   | `assemble_context_pack`              |

## Adding new spans

Use the helper in `cf_knowledge_kiln.api.tracing`:

```python
from cf_knowledge_kiln.api.tracing import get_tracer

_TRACER = get_tracer(__name__)

with _TRACER.start_as_current_span(
    "retrieval.new_phase",
    attributes={"retrieval.foo": "bar"},
) as span:
    result = do_work()
    span.set_attribute("retrieval.result_size", len(result))
```

`get_tracer` returns a real OTel tracer when the `[otel]` extra is
installed and a no-op shim otherwise — there is no `if otel_available`
branch to write at the call site. The shim's `set_attribute` /
`record_exception` calls silently discard. When OTel is installed but
no `TracerProvider` is wired, OTel substitutes its own no-op tracer —
so the cost of an unconfigured tracer is still ~zero.

Name new spans `retrieval.*`, `ingestion.*`, or `api.*` to stay
within the existing namespaces. Attribute keys use dotted
`namespace.field` form.
