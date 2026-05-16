# Phase 0 — Discovery report

Pre-implementation discovery of the surrounding repos, conducted on
2026-05-16.

## Existing repo patterns found

- **`homelab-iac/`** is the user's primary CF-deployment repo. It uses:
  - Plain-Markdown ADRs under `docs/proposals/` with YAML frontmatter
    (status: `canonical | proposal | implemented`). No MADR formalism.
  - `AGENTS.md` as the canonical agent-guidance file with `CLAUDE.md`
    as a symlink. We mirror this here.
  - A `Makefile` for ops-style targets (mostly Ansible deploys).
- **`nexus-agents/`** is the governance substrate. Its `AGENTS.md` is
  the canonical-surface, harness-neutral version of the agent
  guidance, deliberately mirrored across `.cursor`, `.continue`, etc.
  We adopt the same pattern.
- **`cf-local-service-broker/`** provides PostgreSQL via OSBAPI v2,
  but is scoped to a CF-on-kind environment. The user's actual CF is
  **BOSH-on-Incus** (15 CF VMs + 3 Concourse VMs on the R910), not
  kind, so the broker is not in the deployment path for this app.
  PR [cf-local-service-broker#2](https://github.com/williamzujkowski/cf-local-service-broker/pull/2)
  added a pgvector plan there for kind-cluster consumers; it stays
  open but is not load-bearing for kiln.
- **No pgvector-enabled BOSH Postgres release** existed publicly when
  this report was first written (May 2026). The companion
  [`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release)
  is being built in parallel to fill that gap, but **kiln does not
  block on it** — [ADR-0007](./adr/0007-fts-first-embeddings-deferred.md)
  defers embeddings to Phase 5.5, so any standard Postgres binding
  satisfies the MVP.
- **Existing Postgres pattern in homelab-iac:** Authentik runs as a
  Podman container on `pi-a` with `container-authentik-postgres.service`.
  Mentioned for completeness; kiln's MVP only needs stock Postgres,
  which any of the existing BOSH/Podman/managed options satisfies.

## Cloud Foundry patterns found

- Routes follow `<app>.cf-apps.lab.grenlan.com` for the user's
  homelab; we leave the domain configurable rather than baking
  `lab.grenlan.com` in.
- Manifests pin docker images by tag/digest where possible. Our MVP
  uses `python_buildpack` instead because (a) the plan calls for it
  and (b) we have no image-publishing pipeline yet. We can add an
  OCI image build in Phase 7.
- Health-check style: explicit `health-check-type: http` with
  `health-check-http-endpoint: /healthz` and a `health-check-
  invocation-timeout` of 10s.
- Services are bound by name; user-provided services use `cf cups`
  and credential injection from CredHub.
- Two-app split (app + worker) is implied by `homelab-agent` (one
  app, but with a separate `scripts/setup-cf.sh` for ingestion-style
  bootstrapping). We adopt an explicit two-app split per ADR-0004.

## CI/CD patterns found

- `.github/workflows/ci.yml` runs shellcheck, yamllint, markdownlint,
  gitleaks, dns-policy-lint, doc-drift validation, ansible-lint.
- No Concourse pipelines in `homelab-iac` (Concourse itself runs on
  BOSH there). The plan asks for Concourse-compatible CI; we will
  add a `concourse-pipeline.yml` in Phase 8 alongside the GitHub
  Actions one.

## Pre-commit / security patterns found

- `gitleaks` (v8.24.2), `shellcheck-py` (v0.10.0.1), `yamllint`
  (v1.37.0), `ansible-lint`. Versions pinned.
- Local repo hooks (`validate-lab-yml`, `validate-docs`,
  `check-doc-drift`) — none of these apply to our repo, but the
  *pattern* (`repo: local`, `language: system`) does.
- No Python hooks anywhere in `homelab-iac`. We add ruff + mypy +
  pytest baseline since this is the first Python app in the user's
  CF tree.

## UI / UX patterns found

- `homelab-iac` is all infrastructure; no UI patterns apply.
- The plan's UI design is search-first, not chat-first. We honor
  that explicitly in Phase 6.

## API patterns found

- No FastAPI / Express apps exist in `homelab-iac`. The plan's
  OpenAPI 3.1 contract is our anchor. We hand-author the spec for
  contract stability across implementation changes — ADR-0003.

## Reusable code or configs

- `.pre-commit-config.yaml` baseline (gitleaks, shellcheck, yamllint
  versions). Adopted, extended with Python hooks (ruff, mypy) and
  markdownlint.
- `homelab-iac`'s `docs/components/` frontmatter pattern. Adopted in
  spirit (frontmatter for docs that need machine-readable metadata),
  not slavishly.
- `cf-local-service-broker`'s PostgreSQL OSBAPI implementation — the
  exact service we bind against in CF.

## Gaps

- **No FastAPI patterns in the user's tree.** We design fresh.
- **No CF-bound Postgres app yet.** This will be the first; the
  `VCAP_SERVICES` parsing pattern needs to be added in Phase 2.
- **No retrieval-eval harness pattern.** Plan Phase 9 calls for one;
  we will likely scaffold something modest (a `tests/eval/` directory
  with curated query/expected-citation pairs).
- **No agent-context-pack pattern anywhere upstream.** This is a new
  surface; we are designing it to be the reusable reference.

## Proposed implementation path

We agreed on the four-phase staging:

1. **Phase 0** — this report. ✓
2. **Phase 1** — repo skeleton, OpenAPI contract, health endpoints,
   config loader, ADRs 0001–0005, manifest, Procfile, pre-commit,
   tests for what exists.
3. **Phases 2–9** — tracked as GitHub epics (one per phase) with
   child issues drawn from the plan's 20-issue list.

Phase 2 begins with Postgres + pgvector migrations.
