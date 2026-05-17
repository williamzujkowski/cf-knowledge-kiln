# Handoff notes

**As of:** 2026-05-16
**Status:** Phase 0–4 complete; Phase 5 (hybrid retrieval) ready to start. CI green on `main`.

This file is the "where we are, what's next, what's been decided" briefing. For *how* to work in the repo, read [AGENTS.md](./AGENTS.md). For *what* the project is, read [README.md](./README.md). For *the plan*, read [plans/cf-rag-plan.md](./plans/cf-rag-plan.md).

---

## TL;DR

cf-knowledge-kiln is a Cloud Foundry RAG knowledge app — a `cf push`'d Python/FastAPI app that binds to a Postgres + pgvector service and serves cited retrieval to humans (search UI) and AI agents (bounded context packs). Architecture is hybrid retrieval (pgvector similarity + Postgres FTS + metadata ranking) per [ADR-0002](./docs/adr/0002-postgres-pgvector.md) (reaffirmed by [ADR-0008](./docs/adr/0008-pgvector-mvp-critical.md)).

Phase 4 (embeddings) landed: `EmbeddingProvider` Protocol + deterministic `MockEmbeddingProvider`, `OpenAICompatibleEmbeddingProvider` (asyncio semaphore + exponential backoff + secrets stay out of logs), `LocalEmbeddingProvider` (sentence-transformers, lazy-load, runs in a worker thread), config-driven factory with the China-origin exclusion list enforced at load time, `EmbeddingsRepository.upsert` + `existing_hashes_for_document` for content-hash-gated re-embedding, pipeline wired so re-ingestion of unchanged content makes zero provider calls. Also fixed a pre-existing pipeline bug: chunk content edits now upsert on `(document_id, chunk_index)` and orphan chunks are deleted. 128 unit + 33 integration tests green.

Next concrete chunk of work: Phase 5 (hybrid retrieval). Epic [#4](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/4). With chunks + embeddings + FTS in place, Phase 5 wires `/v1/search`, `/v1/answer`, and `/v1/agent/*` over the existing schema. The CF deploy gate still depends on [bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3) (operator runbook).

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

## What's done (Phase 0 + Phase 1 + Phase 2 + Phase 3 + Phase 4)

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

---

## What's next (in order)

### Immediate (Phase 5 — Hybrid retrieval + agent endpoint)

Epic [#4](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/4). Phase 5 wires `/v1/search`, `/v1/answer`, and `/v1/agent/*` over the now-complete index. Inputs ready: chunks with FTS GIN, embeddings with HNSW partial index, hash-keyed `chunk_embeddings` for staleness checks. Ranking signals per the plan; dual response shapes per ADR-0003.

### Parallel (operator track)

- **[bosh-pgvector-release#3](https://github.com/williamzujkowski/bosh-pgvector-release/issues/3)** — runbook for deploying the BOSH release on the homelab director, registering the broker as a CF app, and proving the end-to-end `cf create-service postgresql-local pgvector ...` path works. **Kiln's CF deploy gate** (epic [#1](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/1) acceptance) blocks on this.

### Later phases (epics)

- Phase 3 ([#2](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/2)) — Ingestion pipeline.
- Phase 4 ([#3](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/3)) — Embeddings (active, **not deferred** — ADR-0008 reversed the brief deferral).
- Phase 5 ([#4](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/4)) — Hybrid retrieval + agent context-pack endpoint.
- Phase 6 ([#5](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/5)) — Human search UX (HTMX baseline).
- Phase 7 ([#6](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/6)) — CF packaging polish + HTTP source ingestion with SSRF guard.
- Phase 8 ([#7](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/7)) — CI/CD + security hardening (auth middleware, SBOM/grype, Concourse pipeline).
- Phase 9 ([#8](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/8)) — QA harness + docs polish for forkability.

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

## Open follow-ups (filed as issues; do NOT inline-fix in Phase 2 PRs)

These are tracked omnibus issues from the 2026-05-16 code-reviewer pass. Refer to them when you happen to be in adjacent code; don't make sweep-fixing them the next sprint.

- **[#37 (kiln)](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/37)** — Layer-2 code-review follow-ups: production fail-fast on missing DB URL (Phase 2 candidate), worker exit-code instead of `sleep infinity` (Phase 3 candidate), per-worker connection-pool math docs, OpenAPI contract drift, replacing the homegrown lint_openapi.py with `openapi-spec-validator`, a handful of smaller items.
- **[bosh-pgvector-release#2](https://github.com/williamzujkowski/bosh-pgvector-release/issues/2)** — Layer-2 follow-ups for the BOSH release: extract shared packaging logic, post-start verify hook for `vector.so`, YAML anchor for the pre-commit allowlist, CI tarball-content check, etc.
- **[#34 (kiln)](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/34)** — Public/template readiness review before flipping the repo public.

---

## Traps to avoid

1. **Don't put pgvector in `embeddings` extra.** It's in the `db` extra now. The `embeddings` extra was a transient ADR-0007 artifact; the reversal removed it.
2. **Don't bind kiln to a plain Postgres expecting `CREATE EXTENSION vector` to work at runtime.** The bound role does not have CREATE EXTENSION privilege. The broker (or operator) installs the extension at provision time. The migration assumes the extension is already available.
3. **Don't modify upstream files in `bosh-pgvector-release`.** The `check-upstream-untouched.sh` pre-commit hook will warn you. Per [ADR-0001](https://github.com/williamzujkowski/bosh-pgvector-release/blob/main/docs/adr/0001-fork-and-rebase-policy.md) over there, any change to upstream files goes upstream first.
4. **Don't set `KILN_AUTH_MODE=bearer` in manifest.yml without wiring middleware.** Phase 8 lands the middleware. Until then, claiming bearer auth in env vars is a lie. The current `manifest.yml` deliberately leaves `KILN_AUTH_MODE` unset; there's a comment explaining why.
5. **Don't add new docs/ files without checking markdownlint exclusions.** The current `.markdownlint.json` config is lenient (MD013 line-length off, MD024 siblings_only) but the pre-commit `files:` allowlists in `.pre-commit-config.yaml` scope hygiene hooks to "our additions only" — if you add a file under a new path, you may need to extend the allowlist.
6. **Don't run pip-audit with `--strict --skip-editable`.** Strict refuses editable installs; the CI command is just `--skip-editable`. This was a real failure mode caught earlier.
7. **Don't try to use cf-local-service-broker against a kind cluster you don't have.** The broker repo's README originally framed it as CF-on-kind; that framing was corrected in the merged PR #2 doc-repositioning commit. It works against any CF — read the current README, not your memory of the old one.

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
gh issue view 4  # Phase 5 epic — hybrid retrieval + agent endpoint
git checkout -b feat/phase-5-retrieval
```

Or, to start the operator track in parallel:

```bash
gh issue view 3 -R williamzujkowski/bosh-pgvector-release
# follow the runbook on a session where you can `source ~/deployments/bosh/env.sh`
```
