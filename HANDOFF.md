# Handoff notes

**As of:** 2026-05-16
**Status:** Phase 0 + Phase 1 complete; Phase 2 ready to start. CI green on `main`.

This file is the "where we are, what's next, what's been decided" briefing. For *how* to work in the repo, read [AGENTS.md](./AGENTS.md). For *what* the project is, read [README.md](./README.md). For *the plan*, read [plans/cf-rag-plan.md](./plans/cf-rag-plan.md).

---

## TL;DR

cf-knowledge-kiln is a Cloud Foundry RAG knowledge app — a `cf push`'d Python/FastAPI app that binds to a Postgres + pgvector service and serves cited retrieval to humans (search UI) and AI agents (bounded context packs). Architecture is hybrid retrieval (pgvector similarity + Postgres FTS + metadata ranking) per [ADR-0002](./docs/adr/0002-postgres-pgvector.md) (reaffirmed by [ADR-0008](./docs/adr/0008-pgvector-mvp-critical.md)).

Phase 1 scaffold landed today: FastAPI skeleton with `/healthz`/`/readyz`/`/version`, settings loader, OpenAPI 3.1 contract, CF manifest + Procfile + start scripts, 24-hook pre-commit, GitHub Actions CI, ADRs 0001–0005 + 0007 (superseded) + 0008. Eleven unit tests pass; CI green.

Next concrete chunk of work: Phase 2 (database). Three child issues, do them in order: [#10](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/10) → [#11](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/11) → [#12](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/12).

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

## What's done (Phase 0 + Phase 1)

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

---

## What's next (in order)

### Immediate (Phase 2 — Database)

1. **[#10](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/10)** — asyncpg connection pool + `VCAP_SERVICES` parsing. Local dev reads `KILN_DATABASE_URL`; CF reads the bound service named by `KILN_PG_SERVICE_NAME`. Light up `/readyz` to ping the DB.
2. **[#11](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/11)** — Alembic migrations for the 9 plan-defined tables (`data_sources`, `documents`, `document_chunks`, `chunk_embeddings`, `rag_queries`, `rag_feedback`, `ingestion_runs`, `model_registry`, `context_packs`) + `CREATE EXTENSION IF NOT EXISTS vector` + FTS GIN index + pgvector ANN index.
3. **[#12](https://github.com/williamzujkowski/cf-knowledge-kiln/issues/12)** — Repository layer + integration tests against a real pgvector Postgres (in CI via a service container).

These three can land independently against local-dev pgvector. They do **not** block on the BOSH operator runbook.

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

## How to start working (Phase 2)

```bash
cd /home/william/git/cf-knowledge-kiln
git pull origin main
git checkout -b feat/10-pg-connection-pool

# Local dev with pgvector
docker run -d --name kiln-pg \
  -e POSTGRES_PASSWORD=kiln \
  -e POSTGRES_USER=kiln \
  -e POSTGRES_DB=kiln \
  -p 5432:5432 \
  pgvector/pgvector:pg16
export KILN_DATABASE_URL=postgresql+asyncpg://kiln:kiln@localhost:5432/kiln  # pragma: allowlist secret

# Install with the new db extra
.venv/bin/pip install -e ".[dev,db]"

# Now write the failing test first, then the code (TDD per AGENTS.md)
# tests/unit/test_db_connection.py — pool startup, VCAP_SERVICES parser, /readyz ping
```

`make verify` should be green before every commit. `pre-commit run --all-files` is also useful but slower.

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
gh issue view 10
git checkout -b feat/10-pg-connection-pool
```

Or, to start the operator track in parallel:

```bash
gh issue view 3 -R williamzujkowski/bosh-pgvector-release
# follow the runbook on a session where you can `source ~/deployments/bosh/env.sh`
```
