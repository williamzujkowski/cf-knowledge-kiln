# Handoff notes

**As of:** 2026-05-17
**Status:** Phase 0–5 complete. `/v1/search` and `/v1/agent/context-pack` are live, cited, and persisted to telemetry tables. CI green on `main` (298 unit + 72 integration tests).

This file is the "where we are, what's next, what's been decided" briefing. For *how* to work in the repo, read [AGENTS.md](./AGENTS.md). For *what* the project is, read [README.md](./README.md). For *the plan*, read [plans/cf-rag-plan.md](./plans/cf-rag-plan.md).

---

## TL;DR

cf-knowledge-kiln is a Cloud Foundry RAG knowledge app — a `cf push`'d Python/FastAPI app that binds to a Postgres + pgvector service and serves cited retrieval to humans (search UI) and AI agents (bounded context packs). Architecture is hybrid retrieval (pgvector similarity + Postgres FTS + metadata ranking) per [ADR-0002](./docs/adr/0002-postgres-pgvector.md) (reaffirmed by [ADR-0008](./docs/adr/0008-pgvector-mvp-critical.md)), with ranking + index decisions captured in [ADR-0009](./docs/adr/0009-hybrid-retrieval.md).

**Phase 5 shipped in four slices on 2026-05-17:**

- **Slice 1** (PR [#69](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/69), `ff65268`) — pure-logic retrieval primitives under `src/cf_knowledge_kiln/retrieval/`: `types.py`, `config.py`, `filters.py`, `ranking.py`. 67 unit tests; no DB, no HTTP.
- **Slice 2** (PR [#71](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/71), `dc44c9d`) — `ChunksRepository.hybrid_search` + `search_by_fts` (single CTE per ADR-0009 §5, RRF k=60, vector + FTS arms with filter pushdown). `HybridRetriever.search()` engine. `KILN_HNSW_EF_SEARCH` setting (default 200). 10 new integration tests; HNSW index usage verified via live EXPLAIN ANALYZE.
- **Slice 3** (PR [#72](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/72), `9e2b7ad`) — `HybridRetriever.context_pack()` + new `src/cf_knowledge_kiln/agent/serializers.py` (token budgeting via tiktoken, `UNTRUSTED_CONTENT_NOTICE` preamble, `derive_confidence`, `assemble_context_pack`). Pydantic models for `ContextPackRequest/Response`, `EvidenceChunk`, `RelatedSource`, `TokenBudget`. Direct Pydantic↔hand-spec drift parametrize cases.
- **Slice 4** (PR [#73](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/73), `e6b07bc`) — wired `POST /v1/search` → `SearchResponse` and `POST /v1/agent/context-pack` → `ContextPackResponse`. New `src/cf_knowledge_kiln/api/dependencies.py` for FastAPI `Depends`-able providers (`get_db`, `get_embedding_provider`, `get_retrieval_config`, `get_hybrid_retriever`). Each handler appends a telemetry row to `rag_queries` / `context_packs`. `PHASE_5_ONLY_SCHEMAS` tolerance set is empty now — drift test enforces strict matching for all Phase-5 schemas. Reviewer-caught: telemetry now wrapped in try/except so logging failures don't cascade to 500; excerpt now derived from real chunk content via `SearchResult.chunk_text` (was always empty before).

Bonus from the slice 3 stack: **issue [#70](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/70) closed** — pre-commit ruff bumped to v0.15.13 to match CI's pyproject pin. No more local/CI format drift.

**Next concrete chunk of work (in priority order):**

1. **Issue [#74](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/74)** — collapse `/v1/search` and `/v1/agent/context-pack` to 1 DB session per request (currently each opens 2: retrieval + telemetry). The cleanest fix is to pass the handler's session down into the engine + telemetry writers. The independent reviewer flagged this as HIGH on the slice 4 PR; deferred so slice 4 could land. Small refactor (≤200 lines) — should be one PR. **Recommended starting point** because it's a follow-up to just-merged work, the constraint is visible, and the user already authorized autonomous merging.
2. **Phase 6** (epic [#5](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/5)) — Human search UX (HTMX-on-FastAPI baseline). Child issues: [#23](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/23) UI scaffold, [#24](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/24) result-card component, [#25](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/25) feedback writes to `rag_feedback`. Meaningful pivot to UI work — new templating choice (Jinja vs alternative), HTMX dep, browser testing. Worth a fresh session to plan and may want user input on UI choices.
3. **Phase 7** ([#6](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/6)) — CF packaging polish + HTTP source ingestion with SSRF guard.
4. **Phase 8** ([#7](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/7)) — auth middleware (the `/v1/*` endpoints are currently unguarded; this is deferred-not-bug per the plan).
5. **Phase 9** ([#8](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/8)) — QA harness + docs polish, including [#68](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/68) end-to-end eval harness.

The CF deploy gate still depends on [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3) (operator runbook). Independent of the Phase 6+ work; can be tackled in parallel.

---

## The three repos in scope

You'll likely touch all three at some point. Keep their roles separate.

| Repo | Role | State |
|------|------|-------|
| **`cf-knowledge-kiln`** (this repo) | The RAG CF app. `cf push` deploys it. | Phase 1 scaffold complete; CI green on `main` |
| [**`bosh-pgvector-release`**](https://github.com/williamzujkowski/bosh-pgvector-release) | Public BOSH release providing PostgreSQL + pgvector for any CF foundation that wants it. Fork of `cloudfoundry/postgres-release`. | Buildable release tarball merged on `main`; **operator deployment to the BOSH director still pending** ([#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3)) |
| [**`cf-local-service-broker`**](https://github.com/williamzujkowski/cf-local-service-broker) | OSBAPI v2 broker that exposes the pgvector Postgres as a CF marketplace service. Originally built for CF-on-kind; reframed to work against any CF/BOSH foundation. | `pgvector` plan merged on `main` |

How they fit together (this is the deploy story for kiln on the homelab foundation):

```text
cf-knowledge-kiln (CF app)
  └─ binds to ─→ cf-local-service-broker (CF app, "postgresql-local pgvector" plan)
                   └─ points at ─→ bosh-pgvector-release (BOSH-deployed Postgres + pgvector VM)
```

Local dev and CI sidestep all of this with `docker run pgvector/pgvector:pg16`.

---

## What's done (Phases 0–5 complete)

### Phase 0 — Discovery

- `docs/discovery-report.md` — homelab-iac CF patterns, pre-commit baseline, gaps that need fresh design, the Authentik Podman pattern mention.

### Phase 1 — Skeleton

- Python 3.12 package layout under `src/cf_knowledge_kiln/{api,config,retrieval,ingestion,db,agent}/`. The last four are intentionally empty `__init__.py`s — their import surface is stable so Phase 2+ can fill them in.
- FastAPI app: `/healthz`, `/readyz`, `/version`. CF health check points at `/healthz`.
- Pydantic-settings config loader (`KILN_` env prefix).
- OpenAPI 3.1 hand-authored contract (`openapi/openapi.yaml`) covering all planned human + agent endpoints. The `lint_openapi.py` script gates structural well-formedness.
- 11 unit tests passing.
- `make verify` runs ruff + mypy `--strict` + pytest + openapi-lint. All green.
- CF deployment: `manifest.yml` (two apps: api + worker), `Procfile`, `scripts/start-{api,worker}.sh`.
- 24-hook pre-commit: gitleaks, detect-secrets, ruff, mypy, bandit, shellcheck, shfmt, yamllint, markdownlint, OpenAPI lint, generic hygiene (trailing-whitespace, EOL, etc.).
- GitHub Actions CI: `verify` job (ruff + mypy + pytest + openapi-lint + bandit + pip-audit) + gitleaks + shellcheck + markdownlint.
- ADRs 0001–0005 + 0008 (active), 0007 (superseded), 0002 (reinstated).
- `AGENTS.md` (canonical) + `CLAUDE.md` symlink mirroring homelab-iac.
- Plan copied to `plans/cf-rag-plan.md` for in-repo reference.

### Phase 2 — Database

- **Connection layer** (`src/cf_knowledge_kiln/db/connection.py`) — asyncpg-backed SQLAlchemy async engine, `parse_vcap_services()` for CF bindings, `resolve_database_url()` with explicit-setting > VCAP precedence, `Database` class with `ping()`/`session()`/`dispose()`. Lifespan wiring in `api/app.py` starts/stops the pool around the FastAPI app.
- **`/readyz` Postgres check** — reports `{checks: {postgres: ok|failing}, status: ready|degraded}`. Verified live against direct URL **and** synthetic `VCAP_SERVICES` against a local `pgvector/pgvector:pg16` container.
- **Alembic initial migration** (`alembic/versions/0001_initial_schema.py`) — `CREATE EXTENSION IF NOT EXISTS vector`, all 9 plan tables (`data_sources`, `model_registry`, `documents`, `ingestion_runs`, `document_chunks`, `chunk_embeddings`, `rag_queries`, `rag_feedback`, `context_packs`), FTS GIN on `document_chunks.content`, HNSW partial index on `chunk_embeddings.embedding::vector(768)` for the default model dimension. Reversible `downgrade()`. Refuses cleanly against a non-pgvector Postgres (verified: *"extension 'vector' is not available"*).
- **`vector` column policy** — unconstrained `vector` type so multiple embedding models can coexist; queries filter on `dimensions = N` and cast. Additional partial HNSW indexes per registered model dimension are added in follow-up migrations.
- **SQLAlchemy 2.x ORM models** (`db/models.py`) — typed `DeclarativeBase` mirroring the migration; `pgvector.sqlalchemy.Vector` for the embedding column.
- **9 thin repositories** under `db/repositories/` — `catalog.py`, `documents.py`, `operations.py`, `_base.py`. Each repo exposes `create` / `get` / `list(filters...)` / `delete`. Sessions are owned by the caller for transaction composition.
- **CI integration tier** — new `integration` job in `.github/workflows/ci.yml` provisions `pgvector/pgvector:pg16` as a service container and runs `pytest tests/integration`.

### Phase 3 — Ingestion

- **Source allowlist** (`ingestion/sources.py`) — Pydantic schema for `config/sources.yaml`. Two source types: `git` (repo + branch) and `local` (filesystem path). `SourceAllowlist.from_yaml()` validates structure, rejects duplicate names, and raises a typed `SourceAllowlistError` on bad input. `.get(name)` is the refusal-by-default gate — unlisted names raise `SourceNotAllowedError`.
- **Connectors** (`ingestion/connectors.py`) — `LocalConnector` (directory walk) and `GitConnector` (shallow clone, `--depth=1 --single-branch`). Both enforce per-file size, total repo size, and file-count caps; abort with `IngestionCapExceeded` rather than partially indexing. Non-Markdown files are skipped with typed `SkipReason` (`unsupported_file_type`, `too_large`, `excluded_by_pattern`, `binary_content`).
- **Markdown parsing + chunking** (`ingestion/chunking.py`, `ingestion/tokens.py`) — python-frontmatter for frontmatter, tiktoken (`cl100k_base`) for deterministic token counts, mistune for parse-time validation. Line-based block scanner treats fenced code, GFM tables, list groups, and paragraphs as atomic. Chunks are bounded by H1/H2/H3 boundaries with a configurable 800-token target; oversized sections greedy-pack into multiple chunks without splitting atomic blocks. Every chunk carries `heading_path`, `content_tokens`, and `content_hash` (sha256).
- **Postgres queue** (`alembic/versions/0002_ingestion_jobs.py`, `IngestionJobsRepository`) — new `ingestion_jobs` table + `claim_one()` using `SELECT … FOR UPDATE SKIP LOCKED` so concurrent workers can't double-process a row. Status transitions: queued → running → succeeded | failed (retryable via `requeue`).
- **Worker** (`ingestion/worker.py`) — async polling loop that claims one job per tick, runs the full pipeline (connector → parse → upsert document → upsert chunks with hash-dedup → write ingestion_runs), and marks the job done. SIGTERM-safe (uses `asyncio.Event` shutdown signal).
- **CLI** (`ingestion/cli.py`) — `python -m cf_knowledge_kiln.ingestion {validate | ingest | serve-worker}`. `validate` is the cheap fail-fast for CI / pre-deploy; `ingest` enqueues one `full_resync` job per active allowlisted source; `serve-worker` runs the polling worker.
- **Settings** — three new env vars: `KILN_INGEST_MAX_FILES` (default 10000), `KILN_INGEST_MAX_REPO_BYTES` (default 100 MiB), `KILN_INGEST_POLL_INTERVAL_SECONDS` (default 5.0).
- **Tests** — 83 unit + 28 integration green. Integration tests cover end-to-end local-source ingest (3 markdown files → 3 documents + N chunks → ingestion_runs row), idempotency on unchanged content (re-run does zero chunk creation), cap-violation handling (run marked failed), and SKIP LOCKED concurrency (two sessions each claim a different row).

### Phase 4 — Embeddings

- **Provider abstraction** (`src/cf_knowledge_kiln/ingestion/embedding/__init__.py`) — `EmbeddingProvider` Protocol with `embed(texts) -> list[list[float]]`, `dimensions`, `model`, `provider`, `aclose()`. Deterministic `MockEmbeddingProvider` derives L2-normalized unit vectors from sha256(text) — used by every Phase 4+ test that doesn't need a real backend. No network, no weights.
- **OpenAI-compatible adapter** (`embedding/openai_compatible.py`) — async HTTP client speaking `/v1/embeddings`. `asyncio.Semaphore` cap from `KILN_INGEST_CONCURRENCY`. Exponential backoff + jitter on 408/425/429/5xx; 4xx fails fast. Bearer token attached at client level so it never lands in log messages.
- **Local adapter** (`embedding/local.py`) — sentence-transformers wrapper. Lazy model load, `asyncio.to_thread` for `encode()`. Ships behind the `embeddings` extra (pulls in torch). Tests inject a fake encoder factory so they don't download weights.
- **Config loader + factory** (`embedding/factory.py`) — reads `config/models.yaml`, validates the schema, refuses to start with unknown providers, disabled models, or names matching the excluded prefixes (`qwen`, `deepseek`, `baai/bge`, `bge-`). Required env vars are validated up front. Picks one of `mock | local | openai-compatible`.
- **Repository** — `EmbeddingsRepository.upsert(chunk_id, embedding, model, provider, dimensions, content_hash)` (insert-or-replace on PK) and `existing_hashes_for_document(doc_id) -> {chunk_id: content_hash}` for the pipeline skip-gate.
- **Pipeline integration** (`ingestion/pipeline.py`) — after chunks are upserted, an embedding pass walks each touched document and embeds only the chunks whose stored hash doesn't match. Re-ingestion of unchanged content makes zero provider calls. Embedding failures don't abort the run; they're recorded in `summary.embeddings_failed` and `ingestion_runs.errors`.
- **Pipeline bugfix** — chunk inserts now UPSERT on `(document_id, chunk_index)` and orphan chunks (indices beyond the new content) are deleted. Without this, a content edit would hit `uq_chunks_doc_index` and crash mid-run.
- **Worker integration** — `Worker` accepts an `embedding_provider`; `serve()` builds one from `config/models.yaml` and closes it on shutdown. Missing config = warn + skip embeddings; malformed config = fatal at startup.
- **Settings** — new `KILN_MODELS_CONFIG_PATH` (default `config/models.yaml`).
- **Tests** — 128 unit + 33 integration green. New coverage: protocol contract + deterministic mock (11), openai-compatible adapter incl. concurrency cap + secret-leak guard (9), local adapter incl. lazy-load + off-loop encode (7), config loader + provider factory incl. excluded-list enforcement (14), pgvector upsert + existing-hashes lookup (2), pipeline embeds new chunks (1), pipeline skips re-embedding on unchanged corpus (1), pipeline re-embeds only changed chunks (1).

### Phase 5 prep (eight stacked PRs merged 2026-05-17)

- **[#67](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/67)** — HIGH-severity QA follow-ups: `LocalConnector` resolves symlinks + refuses targets outside the source root (new `symlink_escape` skip reason); `make run-worker` + `make ingest` now invoke the real CLI subcommands instead of failing with `ModuleNotFoundError` / silently validating; README status refreshed from "Phase 1 skeleton" to current; 14 unit + 5 integration tests for `Worker` + 3 integration tests for embedding-failure paths.
- **[#60](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/60)** — Phase 5 design doc + ADR-0009. Captures the seven ranking/index decisions (RRF k=60, `ts_rank_cd`, `hnsw.ef_search = 200`, CTE single-round-trip pattern, filter pushdown, per-dimension HNSW migrations, top-100 → fuse → top-20).
- **[#61](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/61)** — `_build_provider_or_warn` hoisted from `worker.py` to `embedding/factory.py` as `build_provider_from_settings`. Both `worker.serve()` and `api/app.py` lifespan call it; the API now holds an `EmbeddingProvider` in `app.state.embedding_provider`.
- **[#62](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/62)** — Ingest-time prompt-injection scanner. New `ingestion/prompt_injection.py` with `load_phrases` + `scan`. Pipeline stamps `chunk.metadata.has_prompt_injection` + `matched_pattern` so retrieval emits the warning in O(1) per chunk instead of O(N patterns × K chunks) per query.
- **[#63](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/63)** — `/readyz` returns HTTP 503 when any check is `failing` so CF/gorouter routes traffic away. `/healthz` stays 200 unconditionally. Hand-spec also updated to declare both 200 and 503.
- **[#64](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/64)** — Phase 5 contract surface: POST `/v1/search` + POST `/v1/agent/context-pack` return HTTP 501 with a "Phase 5 not implemented" detail. New `tests/unit/test_openapi_drift.py` enforces parity between the hand-authored `openapi/openapi.yaml` and FastAPI's generated `/openapi.json` (paths, methods, operationIds, status codes, schema fields + enums).
- **[#65](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/65)** — `db.redact_dsn(url)` helper. `Database.__init__` logs the redacted URL at INFO so operators see which DB the process is bound to without leaking the password. Handles URL-encoded passwords, IPv6 hosts, query strings, and `None`.
- **[#66](https://github.com/williamzujkowski/cf-knowledge-kiln/pull/66)** — Four integration tests for `IngestionJobsRepository.mark_done` / `mark_failed` / `requeue` / `mark_done(result_run_id)`. **Important pattern for future async repo tests:** use `await session.refresh(claimed)` after a raw-SQL UPDATE + `session.commit()`, not `session.expire_all()`. The latter triggers a sync lazy-load on the next attribute access which the SQLAlchemy 2.0 async layer refuses outside a greenlet (`MissingGreenlet` exception).

### Phase 5 slice 1 (PR #69, commit `ff65268`, merged 2026-05-17)

Pure-logic retrieval primitives. No DB, no HTTP, no embedding provider. All under `src/cf_knowledge_kiln/retrieval/`:

- **`types.py`** — Pydantic models matching `openapi/openapi.yaml` exactly (drift test enforces parity): `Status` Literal, `WarningType` Literal, `RetrievalFilters`, `Warning`, `Conflict`. All use `extra="forbid"`. `Conflict.source_ids` validated `min_length=2`.
- **`config.py`** — `RetrievalConfig` + `load_retrieval_config(path)` reading `retrieval.status_weights` + `freshness.stale_after_days` from `config/security.yaml`. Missing file → defaults + warning; malformed YAML or out-of-range weights → fatal `RetrievalConfigError`. `field_validator` on `status_weights` enforces `0 < weight <= 1.0` (zero rejected because that would silently zero-out a status — operator should use a tiny + number or filter at query time).
- **`filters.py`** — `build_predicates(filters) -> list[ColumnElement[bool]]`. Per-field `IN (…)` for status/doc_type/repo/owner/system/authority/sensitivity; `LIKE 'prefix%' ESCAPE …` for `path_prefix` (autoescape protects against user wildcards); JSONB `?|` "exists any" for tags + control_id; `>=` predicate for `last_reviewed_after`. Empty filter returns `[]` which is a valid no-op.
- **`ranking.py`** — pure functions: `rrf_fuse(arms, k=60)` (Reciprocal Rank Fusion); `apply_boosts(chunks, *, config, today)` (status × freshness multiplier, linear decay past `stale_after_days` with a 0.3 floor at 2× the window); `stale_warnings` / `deprecated_warnings` / `prompt_injection_warnings` / `weak_evidence_warning` emitters, each de-duping per `document_id`; `detect_conflicts(chunks)` (syntactic — same `heading_path` + ≥2 distinct active docs); `requires_human_review(evidence, warnings, conflicts)` — single canonical decision function returning True iff any of: conflict present, empty evidence, all-deprecated, all-draft, prompt_injection/sensitive_content warning, or top score < 0.5.
- **`__init__.py`** — re-exports the public surface so callers can do `from cf_knowledge_kiln.retrieval import HybridRetriever, ...`.
- **Tests** — 67 new unit tests across `tests/unit/test_retrieval_{types,config,filters,ranking}.py`.

### Phase 5 slice 2 (PR #71, commit `dc44c9d`, merged 2026-05-17)

`HybridRetriever.search()` engine + the single-CTE SQL backing it.

- **`db/repositories/_hybrid.py`** (new) — SQL builders for the CTE pattern: `build_hybrid_select` (vector arm via `cast(embedding, Vector(N)).op('<=>')(...)` to honor the partial HNSW index, FTS arm via `func.ts_rank_cd(func.to_tsvector(...), func.plainto_tsquery(...))`, RRF fusion via `SUM(1.0 / (k + rnk))` over UNION ALL); `build_fts_only_select`; `SearchRow` dataclass; `set_local_ef_search`; `row_to_search_row`.
- **`db/repositories/documents.py`** — `ChunksRepository.hybrid_search` and `search_by_fts` methods, each lazy-importing `build_predicates` to break the retrieval↔db cycle.
- **`retrieval/engine.py`** (new) — `HybridRetriever` + `SearchResult` dataclass. `search()` flow: embed → CTE → `apply_boosts` → sort → trim → emit warnings → return.
- **`KILN_HNSW_EF_SEARCH`** setting (default 200) wired into engine via `ef_search=settings.hnsw_ef_search`.
- **Tests** — 10 new integration tests in `tests/integration/test_hybrid_retrieval.py`. **HNSW index usage verified** via live `EXPLAIN ANALYZE` in `docker exec kiln-pg psql ...` — both `embedding::vector(768)` and `CAST(embedding AS VECTOR(768))` produce `Index Scan using ix_chunk_embeddings_hnsw_768`.

### Phase 5 slice 3 (PR #72, commit `9e2b7ad`, merged 2026-05-17)

`HybridRetriever.context_pack()` + agent-shape serialization.

- **`retrieval/types.py`** — added Pydantic models for `ContextPackRequest`, `ContextPackResponse`, `EvidenceChunk`, `RelatedSource`, `TokenBudget`, plus the `Confidence` and `Relationship` Literals. All `extra="forbid"`.
- **`src/cf_knowledge_kiln/agent/serializers.py`** (new): `UNTRUSTED_CONTENT_NOTICE` constant, `DocumentRef` dataclass, `SerializerInputs` bundle dataclass, `trim_evidence_to_budget` (tiktoken-counted prefix; always keeps ≥1 chunk so empty packs never come back for a clear best match), `derive_confidence` (heuristic high/medium/low/none), `assemble_context_pack` (composes the full response, always sets the notice, calls `requires_human_review`).
- **`HybridRetriever.context_pack`** — wires the engine to the serializer. Calls `detect_conflicts` and surfaces results both as structured `Conflict` entries AND `conflicting_sources` warnings (the structured list is canonical for `requires_human_review` per `ranking.py`).
- **Lazy imports** in `engine.context_pack` + `_document_refs_from_rows` break the retrieval↔agent cycle.
- **Drift test tightening** — new `TestPydanticModelsMatchHandSpec` parametrize-checks each Pydantic model's required-field + property-name sets against `openapi.yaml` directly via `model_json_schema()`. Catches drift even when the model isn't yet wired to a route.
- **Hand-spec fix** — `EvidenceChunk.source_url` dropped from required (real ingested docs often have no URL).
- **Bonus**: **issue #70 closed** by bumping pre-commit ruff to v0.15.13 (CI uses pyproject's `>=0.7,<1.0`, which installs v0.15.x). The two were drifting; aligned now.
- **Tests** — +27 unit serializer + 17 unit type/drift + 7 integration `context_pack`.

### Phase 5 slice 4 (PR #73, commit `e6b07bc`, merged 2026-05-17)

The 501 stubs are gone. `/v1/search` and `/v1/agent/context-pack` are live, cited, and persisted.

- **`api/dependencies.py`** (new) — FastAPI `Depends`-able providers: `get_db` (503 if unbound), `get_embedding_provider`, `get_retrieval_config`, `get_hybrid_retriever`.
- **`api/retrieval.py`** — `POST /v1/search` → `SearchResponse`, `POST /v1/agent/context-pack` → `ContextPackResponse`. Each handler appends a row to `rag_queries` / `context_packs` via the corresponding repository. **Telemetry writes wrapped in `try/except logger.exception`** so a transient DB failure during logging doesn't turn a successful retrieval into a 500 (reviewer-caught HIGH).
- **`retrieval/types.py`** — added `SearchRequest`, `SearchResponse`, `ResultCard` Pydantic models. `ResultCard.source_url` optional; `ResultCard.score` no longer capped at 1 (post-boost RRF / FTS-only `ts_rank_cd` can exceed it).
- **`retrieval/engine.SearchResult`** now also carries `document_refs` (UUID → DocumentRef) AND `chunk_text` (UUID → raw content). The handler uses these for ResultCard's title/repo/path/etc. and for the `excerpt` field (previously the excerpt was always empty because `chunk.chunk_metadata` only carries ingest-time prompt-injection flags — reviewer-caught MEDIUM).
- **Hand-spec** — dropped the dummy `501` responses from `/v1/search` + `/v1/agent/context-pack`; relaxed `ResultCard.score.maximum`; updated descriptions.
- **Drift test** — `PHASE_5_ONLY_SCHEMAS` is now empty (strict matching applies uniformly). `_enum_or_const` updated to resolve Pydantic's `Optional[Enum]` shape (`anyOf: [{$ref}, {type: null}]`) so optional enum fields don't false-negative against the hand-spec's flat enum form.
- **Deleted** `tests/unit/test_retrieval_stubs.py` (501-stub assertions no longer match runtime).
- **Tests** — +7 integration in `tests/integration/test_api_routes.py`. Smoke test asserts real title (not the `"(unknown)"` defensive fallback) and non-empty excerpt — these prove `document_refs` + `chunk_text` actually plumb data through.

---

## What's next (in order)

### Immediate: issue [#74](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/74) — collapse to 1 DB session per request

Reviewer-flagged on slice 4. Each `/v1/search` and `/v1/agent/context-pack` request opens **two** sessions: one inside `HybridRetriever._fetch_candidates` for the hybrid CTE (transactional for `SET LOCAL hnsw.ef_search`) and a second inside `_log_rag_query` / `_log_context_pack` for telemetry. With `KILN_PG_POOL_SIZE=5` + `KILN_PG_POOL_MAX_OVERFLOW=10` (15 connections total), the API ceilings at ~8 concurrent requests.

**Recommended fix (option B in #74):** Have the API handler open one session, pass it as an optional parameter to `HybridRetriever.search` / `.context_pack`, and have the retriever use it when provided (falling back to `db.session()` only when called outside an HTTP context). Telemetry repos take the same session. The `SET LOCAL` still scopes correctly because both retrieval and logging live inside the same transaction.

**File plan:**

1. Extend `HybridRetriever.search` + `context_pack` signatures with `session: AsyncSession | None = None`. When None, open one as today.
2. Refactor `_fetch_candidates` to use the provided session if any, else open one.
3. API handlers in `api/retrieval.py` open one session via a new `Depends(get_session)` dep (or via `async with db.session() as session, session.begin():` at the top of each handler) and pass it through both engine + telemetry.
4. Add an integration test that asserts the connection count under load (or at least verifies a single transaction by mocking `db.session()` and counting calls).

This is a follow-up to just-merged work; small refactor (≤200 lines), one PR. User pre-authorized autonomous merging.

### Then: Phase 6 — Human search UX (epic [#5](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/5))

HTMX-on-FastAPI baseline. Child issues:

- **[#23](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/23)** — Search box, filter panel, result list, document preview. Server-rendered Jinja templates + HTMX for partial updates. No build step. Acceptance: user can search a fixture corpus and see cited results; filters narrow without page reload; deprecated results visually flagged; browser smoke-tested.
- **[#24](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/24)** — Result card component with status badge, freshness indicator, source link.
- **[#25](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/25)** — Feedback buttons that write to `rag_feedback`.

**Pivot worth flagging to the user before starting**: this is the first UI work in the repo. Choices that benefit from input: templating engine (Jinja2 is the obvious FastAPI default), styling approach (vanilla CSS vs Tailwind), HTMX version pin. Suggest a quick read of [docs/user-journeys.md](./docs/user-journeys.md) for the intended human flow.

### Parallel (operator track)

- **[bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3)** — runbook for deploying the BOSH release on the homelab director, registering the broker as a CF app, and proving the end-to-end `cf create-service postgresql-local pgvector ...` path works. **Kiln's CF deploy gate** (epic [#1](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/1) acceptance) blocks on this.

### Later phases (epics)

- Phase 7 ([#6](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/6)) — CF packaging polish + HTTP source ingestion with SSRF guard.
- Phase 8 ([#7](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/7)) — CI/CD + security hardening (auth middleware lands here — the `/v1/*` endpoints are currently unguarded by design until then; current `manifest.yml` deliberately leaves `KILN_AUTH_MODE` unset).
- Phase 9 ([#8](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/8)) — QA harness + docs polish for forkability; includes [#68](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/68) (end-to-end evaluation harness for human + agent user journeys).

---

## Key decisions (don't re-litigate these)

### ADR-0008 supersedes ADR-0007 — pgvector is back in MVP

**This one is the most likely to confuse future-you.** Walking through the timeline:

- **ADR-0002 (accepted, then briefly superseded, then reinstated):** Postgres + pgvector as the retrieval store. The plan calls for it.
- **ADR-0007 (superseded, same day):** FTS-first; embeddings deferred to a Phase 5.5 decision gated on Phase 9 eval results. Reasoning was: of 9 plan ranking signals only 1 is semantic, and pgvector deployment cost on the homelab BOSH was expensive.
- **ADR-0008 (active):** Reverses ADR-0007. Owner clarified that kiln is intended to ship as a pgvector-backed RAG CF app from MVP. The pgvector deployment cost also dropped to operator-runbook level when `bosh-pgvector-release` shipped same-day.

What this means in practice:

- The 9-table schema (incl. `chunk_embeddings` and `model_registry`) is Phase 2 scope, not Phase 5.5 scope.
- Phase 4 (Embeddings) is active work, not deferred.
- Phase 5 (Retrieval) is hybrid from day one, not "FTS now, vector later."
- The Phase 5.5 decision issue ([#36](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/36)) is closed — resolved early.

The ADR-0007 file is preserved on disk as a historical record of the temporarily-considered alternative. Don't delete it. Don't try to re-derive its conclusions either — it was already reversed.

### Other anchors that already have ADRs

- **[ADR-0001](./docs/adr/0001-use-python.md)** — Python 3.12, FastAPI. Decided early; no reason to revisit.
- **[ADR-0003](./docs/adr/0003-openapi-and-dual-shape.md)** — OpenAPI 3.1, hand-authored, separate human and agent shapes. The OpenAPI contract is load-bearing. There's a Phase 5 test that diffs hand-authored vs FastAPI-generated specs.
- **[ADR-0004](./docs/adr/0004-cf-process-model.md)** — Two CF apps (api + worker), both bound to the same Postgres.
- **[ADR-0005](./docs/adr/0005-model-provider-abstraction.md)** — Models are config, not code. China-origin exclusion list (Qwen / DeepSeek / BAAI/BGE) is enforced via `docs/model-providers.md` review.

---

## How to start working (Phase 5)

```bash
cd /home/william/git/cf-knowledge-kiln
git pull origin main
git checkout -b feat/phase-5-retrieval

# Local dev with pgvector (already created in Phase 2; re-start if stopped)
docker start kiln-pg
export KILN_DATABASE_URL=postgresql+asyncpg://kiln:kiln@localhost:5432/kiln  # pragma: allowlist secret

# Phase 5 reuses everything already installed. The `embeddings` extra
# is only needed if you want to use the `local` provider for smoke tests;
# the mock provider works without it.
.venv/bin/pip install -e ".[dev,db,ingestion]"

# Apply migrations (idempotent if already at head)
.venv/bin/python -m alembic upgrade head

# Smoke-test the ingestion + embedding pipeline against fixtures
# (point KILN_MODELS_CONFIG_PATH at a YAML with provider: mock)
.venv/bin/python -m cf_knowledge_kiln.ingestion validate --config config/sources.example.yaml
```

`make verify` should be green before every commit. `pre-commit run --all-files` is also useful but slower. Integration tests run via `pytest tests/integration -q` against the live `kiln-pg` container.

---

## Open follow-ups (filed as issues; do NOT inline-fix unless you happen to be in adjacent code)

- **[#74](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/74)** — `/v1/search` and `/v1/agent/context-pack` open 2 DB sessions per request (retrieval + telemetry). Caps practical concurrency. Recommended fix in the issue body. **This is the recommended next chunk of work.**
- **[#37 (kiln)](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/37)** — Layer-2 code-review follow-ups from the 2026-05-16 pass: production fail-fast on missing DB URL, worker exit-code instead of `sleep infinity`, per-worker connection-pool math docs, OpenAPI contract drift items, replacing the homegrown `lint_openapi.py` with `openapi-spec-validator`, a handful of smaller items.
- **[#47](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/47)** — `Worker._process` opens two sessions; a mid-process crash leaves the job in `running` until the recovery sweep requeues it (which then redoes idempotent work). Owner-input design decision. (Related to #74 — both about 2-session-per-operation patterns.)
- **[#49](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/49)** — DRY the create-and-refresh + filtered-list pattern across 9 repos. Mixin or generic base.
- **[#53, #54](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/53)** — Low-priority code cleanup and test-coverage bundles. Bite-sized.
- **[#55](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/55)** — Add CODEOWNERS + evaluate Semgrep/CodeQL alongside existing bandit.
- **[#68](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/68)** — End-to-end evaluation harness for human + agent user journeys (Phase 9 scope).
- **[#19 (kiln)](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/19)** — Body still references the reversed ADR-0007 ("FTS-only"). Comment posted; needs owner edit.
- **[#34 (kiln)](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/34)** — Public/template readiness review before flipping the repo public.
- **[bosh-pgvector-release#2](https://github.com/williamzujkowski/bosh-pgvector-release/issues/2)** — Layer-2 follow-ups for the BOSH release.

Closed during Phase 5: #70 (ruff pre-commit/CI version drift, fixed in slice 3 by pinning pre-commit to ruff v0.15.13).

---

## Traps to avoid

1. **Don't put pgvector in `embeddings` extra.** It's in the `db` extra now. The `embeddings` extra was a transient ADR-0007 artifact; the reversal removed it.
2. **Don't bind kiln to a plain Postgres expecting `CREATE EXTENSION vector` to work at runtime.** The bound role does not have CREATE EXTENSION privilege. The broker (or operator) installs the extension at provision time. The migration assumes the extension is already available.
3. **Don't modify upstream files in `bosh-pgvector-release`.** The `check-upstream-untouched.sh` pre-commit hook will warn you. Per [ADR-0001](https://github.com/williamzujkowski/bosh-pgvector-release/blob/main/docs/adr/0001-fork-and-rebase-policy.md) over there, any change to upstream files goes upstream first.
4. **Don't set `KILN_AUTH_MODE=bearer` in manifest.yml without wiring middleware.** Phase 8 lands the middleware. Until then, claiming bearer auth in env vars is a lie. The current `manifest.yml` deliberately leaves `KILN_AUTH_MODE` unset; there's a comment explaining why.
5. **Don't add new docs/ files without checking markdownlint exclusions.** The current `.markdownlint.json` config is lenient (MD013 line-length off, MD024 siblings_only) but the pre-commit `files:` allowlists in `.pre-commit-config.yaml` scope hygiene hooks to "our additions only" — if you add a file under a new path, you may need to extend the allowlist.
6. **Don't run pip-audit with `--strict --skip-editable`.** Strict refuses editable installs; the CI command is just `--skip-editable`. This was a real failure mode caught earlier.
7. **Don't try to use cf-local-service-broker against a kind cluster you don't have.** The broker repo's README originally framed it as CF-on-kind; that framing was corrected in the merged PR #2 doc-repositioning commit. It works against any CF — read the current README, not your memory of the old one.
8. **Don't `git pull` after a squash-merge of a stacked PR without also `git reset --hard origin/main`.** When a parent PR is squashed and its branch deleted, the child PR auto-closes; the workaround is to retarget the child to main via `gh api repos/.../pulls/N -X PATCH -f base=main`, rebase the child onto main (git's cherry-pick detection auto-drops the already-applied parent commits), force-push, re-dispatch CI. Pattern was exercised end-to-end on PRs #56→#67 / #60–#66 / #69; works fine but takes time.
9. **GitHub Actions PR `synchronize` events aren't firing reliably on this repo** (cause unknown — likely a setting or rate-limit). Use `gh workflow run ci.yml --ref BRANCH` after each push to force a CI run that associates with the PR. This pattern is now baked into the workflow yaml's `workflow_dispatch` trigger.
10. **Don't use `session.expire_all()` after a raw-SQL UPDATE in async tests.** It triggers a sync lazy-load on the next attribute access; SQLAlchemy 2.0 async refuses it with `MissingGreenlet`. Use `await session.refresh(claimed_row)` instead. See PR #66 for the pattern.
11. **Don't wire ANYTHING in `retrieval/__init__.py` that imports a module which back-references `retrieval`.** Three Phase-5 PRs hit this cycle (slice 2's `documents.py → retrieval.filters`, slice 3's `engine.py → agent.serializers → retrieval.ranking`). The fix that has worked twice: lazy-import the offending name *inside* the method/function that needs it, and either gate type annotations behind `TYPE_CHECKING` or use `Any` (since `from __future__ import annotations` is everywhere, annotations are strings already).
12. **Persist UUIDs to JSONB columns with `model_dump(mode="json")`, not raw `model_dump()`.** Default Pydantic dump returns `UUID` instances that asyncpg's JSON encoder rejects. The `_log_context_pack` helper in `api/retrieval.py` shows the pattern.
13. **Telemetry writes must NOT cascade to 500s.** Wrap any "log this query happened" code in `try/except logger.exception` — a transient DB failure during telemetry is not a user-visible error. Slice 4's `_log_rag_query` and `_log_context_pack` are the canonical examples.
14. **Slice tests that exercise warning paths must assert the precondition.** Several slice 2/3/4 tests originally said `if X in result: assert warning` — which passes vacuously if `X` isn't returned. Use `assert X` first to prove the precondition fires, then assert the warning. Reviewer caught this on slice 2.
15. **The score field is unbounded.** Hand-spec used to cap `ResultCard.score` at 1.0; that's not true — FTS-only fallback uses raw `ts_rank_cd` which can exceed it. Slice 4 dropped the cap. Clients that validated the old schema strictly may need updates.

---

## Quick links

- **Architecture overview:** [docs/architecture.md](./docs/architecture.md)
- **User journeys (human + agent):** [docs/user-journeys.md](./docs/user-journeys.md)
- **Deployment (CF):** [docs/deployment-cloud-foundry.md](./docs/deployment-cloud-foundry.md)
- **All ADRs:** [docs/adr/README.md](./docs/adr/README.md)
- **Plan (original):** [plans/cf-rag-plan.md](./plans/cf-rag-plan.md)
- **All open issues:** `gh issue list -R williamzujkowski/cf-knowledge-kiln`
- **All open issues across the three repos:** `for r in cf-knowledge-kiln bosh-pgvector-release cf-local-service-broker; do echo "=== $r ==="; gh issue list -R "williamzujkowski/$r" --state open --json number,title --jq '.[] | "  #\(.number) \(.title)"' | head; done`

---

## Suggested first move

```bash
cd /home/william/git/cf-knowledge-kiln
git checkout main && git pull
docker start kiln-pg  # pgvector container; ignore "already running"
export KILN_DATABASE_URL=postgresql+asyncpg://kiln:kiln@localhost:5432/kiln  # pragma: allowlist secret
PY=.venv/bin/python make verify             # 298 unit baseline
.venv/bin/pytest tests/integration -q       # 72 integration baseline
```

For the next chunk (#74 — collapse to one DB session per request):

```bash
git checkout -b fix/74-one-session-per-request
gh issue view 74              # read the recommended fix
# Touch points:
#   src/cf_knowledge_kiln/retrieval/engine.py  (signature + _fetch_candidates)
#   src/cf_knowledge_kiln/api/retrieval.py     (handler opens session, passes it down)
#   src/cf_knowledge_kiln/api/dependencies.py  (add get_session dep)
#   tests/integration/test_api_routes.py       (add concurrency assertion)
```

For Phase 6 (HTMX UI), ask the user about templating + styling choices first — this is the first UI work in the repo:

```bash
gh issue view 5                # epic
gh issue view 23               # first child (search UI scaffold)
cat docs/user-journeys.md      # intended human flow
```

Or, to start the operator track in parallel:

```bash
gh issue view 3 -R williamzujkowski/bosh-pgvector-release
# follow the runbook on a session where you can `source ~/deployments/bosh/env.sh`
```
