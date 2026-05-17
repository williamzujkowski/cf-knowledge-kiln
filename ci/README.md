# Concourse pipeline (#30)

`concourse-pipeline.yml` is the CF-foundation-native CI/CD mirror of `.github/workflows/ci.yml`. Operators who run their own Concourse can target it at this repo (or any fork) and get the same gates as GitHub Actions plus a manually-gated deploy stage.

## Jobs

| Job | What it runs | Mirrors GH job |
|---|---|---|
| `verify` | `ruff check` + `ruff format --check` + `mypy --strict` + `pytest tests/unit` + `lint_openapi.py` + `bandit` + `pip-audit` | `verify` |
| `integration` | `pytest tests/integration` against `kiln-test-db-url` | `integration` |
| `gitleaks` | `gitleaks detect --no-banner --redact` | `secrets-scan` |
| `shellcheck` | `shellcheck scripts/*.sh` | `shellcheck` |
| `markdownlint` | `markdownlint-cli2` with the same globs/config as GH | `markdown-lint` |
| `sbom-scan` | `syft` → SPDX SBOM → `grype --fail-on high` | `sbom-scan` |
| `deploy-cf` | Manual gate; `cf push -f manifest.yml` once every CI job is green | (no GH equivalent — deploy is a separate workflow there) |

## Setting the pipeline

```bash
fly -t <target> set-pipeline \
    -p cf-knowledge-kiln \
    -c ci/concourse-pipeline.yml \
    --load-vars-from ci/vars.yml
```

Validate syntax without applying:

```bash
fly -t <target> validate-pipeline -c ci/concourse-pipeline.yml
```

Unpause the pipeline so the auto-trigger jobs (`verify`, `integration`, the linters, `sbom-scan`) start running on each commit:

```bash
fly -t <target> unpause-pipeline -p cf-knowledge-kiln
```

The `deploy-cf` job has `trigger: false` on its `get`, so it stays manual:

```bash
fly -t <target> trigger-job -j cf-knowledge-kiln/deploy-cf
```

## Required vars

See [`vars.example.yml`](./vars.example.yml). Copy to `vars.yml` (gitignored) and customize.

Secret-bearing vars (`kiln-test-db-url`, `cf-password`) MUST come from a credential manager (Vault, credhub, ...). Never commit them; never pass via plain `--var` on a shared pipeline.

## Integration-tier DB

The `integration` job needs a real Postgres+pgvector. Concourse tasks aren't a great fit for sidecar containers, so the pipeline takes the CF-idiomatic path: operators bring their own bound DB via `kiln-test-db-url`. Two patterns:

- **Per-foundation static DB.** Provision a small pgvector Postgres in your foundation, expose its DSN through your credential manager, point `kiln-test-db-url` at it. Tests `TRUNCATE` between cases — see `tests/integration/conftest.py::_truncate_between_tests` — so isolation across CI runs is fine.
- **Per-build ephemeral DB.** Wire a `create-service` / `delete-service` task pair around the `integration` job. More involved; not in scope for this PR.

## Why not docker-compose / sidecar containers

Concourse tasks are single-container by design. The community workarounds (`oci-build-task` + spawning a sidecar inside the task) are fragile and version-sensitive. A bound DB matches CF operational reality — production Postgres is always a service binding, never a sidecar.

## Differences from GitHub Actions

- **No `permissions:` block.** Concourse uses worker / team ACLs instead.
- **No `concurrency:` cancel.** Concourse uses `serial: true` per job; the homelab target's small worker count makes serial execution the safer default.
- **No CodeQL job.** CodeQL is GitHub-native; the closest Concourse equivalent is `bandit` (already in `verify`) plus `grype` (in `sbom-scan`). Operators who want richer SAST should add a `semgrep` task using `returntocorp/semgrep` image.

## Validation status

- [x] Pipeline parses with `fly validate-pipeline -c ci/concourse-pipeline.yml` (per the issue acceptance criterion).
- [ ] End-to-end run on a real Concourse — operator-side validation; tracked as part of bringing the pipeline online in your foundation.
