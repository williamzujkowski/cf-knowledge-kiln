# Forking guide

This document walks a second team — call them **Team Beta** — through forking `cf-knowledge-kiln` and standing it up against their own Cloud Foundry foundation, their own document corpus, and their own embedding provider. Every step has a copy-pasteable command. The whole walkthrough takes about 90 minutes if your foundation is already healthy.

The short-form happy path lives in the [README](../README.md#fork--repurpose); this document is the deeper guide with troubleshooting, foundation variations, and validation gates.

---

## Who this is for

A platform engineer or operator who:

- has push access to a Cloud Foundry foundation (any cloud provider, any CF distro that speaks OSBAPI v2);
- can bind a Postgres service that exposes the `pgvector` extension;
- maintains internal documentation in one or more allowlisted sources (Git repos, HTTP roots, or local trees);
- wants a cited human search UI **and** a bounded agent context-pack API over that documentation.

You do **not** need to fork unless you intend to change behavior. The repo is permissively MIT-licensed and the model registry is config-driven, so most teams can stand up their own instance without touching code. Fork only when you need a code change.

---

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| `cf` CLI | v8+ | The push target |
| `bosh` CLI | v7+ | Only if you're deploying `bosh-pgvector-release` yourself |
| Python | 3.12+ | Buildpack target + dev loop |
| `docker` | any recent | Local pgvector for dev/CI |
| `gh` CLI | optional | Driving GitHub interactions |

Verify your CF target is logged in and points where you intend:

```bash
cf target
cf api
```

---

## Step 1 — Fork and rename

Fork the repo through GitHub's UI or via `gh`:

```bash
gh repo fork williamzujkowski/cf-knowledge-kiln \
  --clone --org team-beta --fork-name knowledge-kiln
cd knowledge-kiln
```

Now scrub every place the upstream's GitHub URL is hard-coded. Pick a single canonical replacement and use `sed`:

```bash
OLD=williamzujkowski/cf-knowledge-kiln
NEW=team-beta/knowledge-kiln

# Files that hard-code the upstream URL or owner handle:
grep -rl "$OLD" \
  AGENTS.md pyproject.toml manifest.yml \
  SECURITY.md CODE_OF_CONDUCT.md \
  openapi/openapi.yaml .github/CODEOWNERS \
  | xargs sed -i "s|$OLD|$NEW|g"

# Owner handle (your CODEOWNERS team or user):
sed -i 's|@williamzujkowski|@team-beta/platform|g' .github/CODEOWNERS

# AGENTS.md "Owner:" field — replace with whoever maintains the fork.
sed -i 's|^\*\*Owner:\*\* .*|**Owner:** @team-beta/platform|' AGENTS.md
```

Update the package name in `pyproject.toml` if you want imports to come in as `team_beta_kiln` instead of `cf_knowledge_kiln`. This is a heavier change — most forks skip it and keep the upstream module name as a recognition aid.

`HANDOFF.md` is the upstream maintainer's operational state. Treat it as starting context for your fork and rewrite as your own work diverges, or delete it and start a fresh `HANDOFF.md` documenting Team Beta's work.

Commit the rename:

```bash
git add -A
git commit -m "fork: rename to team-beta/knowledge-kiln"
git push -u origin main
```

**Validation:** `grep -r williamzujkowski .` should return only matches inside `CHANGELOG.md` and `docs/adr/` (immutable historical records — leave them alone).

---

## Step 2 — Choose your Cloud Foundry foundation

The upstream was built and tested on a homelab BOSH foundation. The guide below is foundation-agnostic; pick the path that matches yours.

### 2a. SAP BTP / Anynines / TAS / commercial foundations

You already have a marketplace. Find a Postgres service that exposes `pgvector`:

```bash
cf marketplace -e postgresql
```

If the marketplace plan does not include `pgvector` by default, your operator will need to enable it on the broker side. `pgvector` requires the database to run `CREATE EXTENSION vector` at provision time; some commercial plans gate this behind a flag (look for "extensions" in the plan metadata).

### 2b. Operator-owned BOSH foundation

The upstream ships [`bosh-pgvector-release`](https://github.com/williamzujkowski/bosh-pgvector-release) and [`cf-local-service-broker`](https://github.com/williamzujkowski/cf-local-service-broker). Together they expose a `postgresql-local pgvector` plan to your marketplace. Deploy in this order:

1. `bosh-pgvector-release` → BOSH VM with PostgreSQL 16 + pgvector.
2. `cf-local-service-broker` → CF app that brokers the BOSH-deployed Postgres.
3. `cf create-service-broker` + `cf enable-service-access` to publish the plan.

That sequence is captured in the respective repos' READMEs and is outside the scope of this guide.

### 2c. No CF foundation — running on Kubernetes

Strictly out of scope. The repo's only CF-specific surface is `manifest.yml`, `Procfile`, and the `VCAP_SERVICES` parsing in `cf_knowledge_kiln/db/connection.py`. You can adapt without too much pain, but you're on your own; nothing in this guide is k8s-validated.

---

## Step 3 — Provision Postgres + pgvector

Create the service instance using whichever broker you've chosen:

```bash
# Pattern: cf create-service <service-name> <plan-name> <instance-name>
cf create-service postgresql pgvector cf-knowledge-kiln-db
```

Replace `cf-knowledge-kiln-db` with whatever name you've put in `manifest.yml` under `services:`. The default in the upstream is `cf-knowledge-kiln-db`; many forks keep it.

Wait for the instance to be ready:

```bash
cf service cf-knowledge-kiln-db
# Watch for: "status: create succeeded"
```

**Validation:** push a tiny test app that binds and runs `SELECT extname FROM pg_extension WHERE extname = 'vector';` (one line; not worth scripting). If it returns a row, the bind is wired and pgvector is live.

For local dev / CI, the integration tests assume a `pgvector/pgvector:pg16` container on `localhost:5432`:

```bash
docker run -d --name kiln-pg \
  -e POSTGRES_PASSWORD=kiln -e POSTGRES_USER=kiln \
  -e POSTGRES_DB=kiln -p 5432:5432 \
  pgvector/pgvector:pg16
```

The default test DSN matches these credentials; see `tests/integration/conftest.py:DEFAULT_TEST_DSN`.

---

## Step 4 — Configure your sources

The ingestion pipeline refuses to fetch any source not in `config/sources.yaml`. Copy the example and edit:

```bash
cp config/sources.example.yaml config/sources.yaml
$EDITOR config/sources.yaml
```

Three source kinds are supported (see `src/cf_knowledge_kiln/ingestion/sources.py` for the full schema):

- **`git`** — public or token-authenticated Git repo. Shallow-cloned at ingest time. The standard pattern.
- **`http`** — single URL (e.g., a documentation index) with SSRF + DNS-pinning guards. Use for vendor docs that aren't in Git.
- **`local`** — a filesystem path. Use for dev fixtures or air-gapped corpora.

A typical Team Beta entry:

```yaml
sources:
  - name: platform-runbooks
    type: git
    repo: team-beta/platform-runbooks
    branch: main
    include:
      - "docs/**/*.md"
      - "runbooks/**/*.md"
    exclude:
      - "docs/generated/**"
    status: active                    # active | draft | deprecated
    authority: standard               # standard | canonical | experimental
    default_owner: platform
    default_sensitivity: internal
```

`status` drives retrieval ranking — `deprecated` documents are visibly flagged in result cards and excluded from agent context packs by default.

**Validation:** `make ingest` should pick up the new source on the next worker run.

```bash
make ingest
# Then check the ingestion_runs table:
psql "$DATABASE_URL" -c "SELECT source_id, status, stats FROM ingestion_runs ORDER BY started_at DESC LIMIT 5;"
```

---

## Step 5 — Choose your embedding + generator models

`config/models.yaml` declares which embedding model and (optionally) which generator model the app uses. The default is a local sentence-transformers model that ships zero external dependencies:

```bash
cp config/models.example.yaml config/models.yaml
```

The default embedding entry — `nomic-embed-text-v1.5`, 768 dimensions — works out of the box and runs on CPU. Two reasons to switch:

1. **You want a managed provider.** Set `provider: openai-compatible` and bind the keys via `cf set-env`:

   ```bash
   cf set-env cf-knowledge-kiln-api KILN_EMBEDDING_BASE_URL https://your-provider.example.com/v1
   cf set-env cf-knowledge-kiln-api KILN_EMBEDDING_API_KEY  '<secret>'
   cf restage cf-knowledge-kiln-api
   ```

2. **You need different embedding dimensions.** This is a one-time migration: the `chunk_embeddings` table has a fixed `vector(N)` column. Changing `dimensions` requires an Alembic migration and a full reindex. See `docs/architecture.md` for the per-dimension HNSW strategy.

The repo enforces a US-origin allowlist on embedding model weights — Qwen, DeepSeek, and BAAI/BGE are refused at load time. See [`docs/model-providers.md`](./model-providers.md) for the rationale and the current allowlist.

The generator model is disabled by default (`enabled: false`). It only matters when you wire up `/v1/answer` (post-MVP). Until then, leave it off; the retrieval surface works without it.

---

## Step 6 — Wire authentication

Production refuses to start without explicit auth. The default mode is **bearer** — a single shared secret in the `Authorization: Bearer <token>` header.

```bash
# Generate a high-entropy token (32+ chars):
TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
cf set-env cf-knowledge-kiln-api KILN_AUTH_MODE   bearer
cf set-env cf-knowledge-kiln-api KILN_BEARER_TOKEN "$TOKEN"
cf restage cf-knowledge-kiln-api
```

Save the token somewhere your callers can read it. The middleware uses `secrets.compare_digest` for constant-time comparison; do **not** echo the token to logs.

If your foundation has the gorouter in front and you want the per-IP rate limit to key on real client IPs rather than the gorouter's address, flip the XFF trust setting:

```bash
cf set-env cf-knowledge-kiln-api KILN_TRUST_FORWARDED_FOR true
cf restage cf-knowledge-kiln-api
```

Leave it `false` for any deployment where callers can reach the dyno directly without an upstream proxy stripping `X-Forwarded-For`.

mTLS-mode auth is a follow-up; see issue #29 follow-ups in the upstream.

---

## Step 7 — Deploy

```bash
make cf-push
```

This invokes `cf push -f manifest.yml`, which creates two apps:

- `cf-knowledge-kiln-api` — request-serving FastAPI app, bound to your Postgres service.
- `cf-knowledge-kiln-worker` — ingestion worker. No route; pulls work from the `ingestion_jobs` queue.

Watch the start logs:

```bash
cf logs cf-knowledge-kiln-api --recent
```

The first request that hits an unmigrated DB will trigger Alembic; if you prefer to run migrations explicitly:

```bash
cf ssh cf-knowledge-kiln-api -c "cd /home/vcap/app && make migrate"
```

**Smoke test:**

```bash
./scripts/smoke-test.sh https://cf-knowledge-kiln-api.<your-domain>
```

The script honors `KILN_AUTH_MODE=bearer` — set the same `KILN_BEARER_TOKEN` you used on the API for the test to pass.

---

## Step 8 — Seed the corpus

Enqueue ingestion for every source in your allowlist:

```bash
cf ssh cf-knowledge-kiln-worker -c "cd /home/vcap/app && make ingest"
```

The worker processes jobs from the `ingestion_jobs` queue with smart crash recovery (`result_run_id` checkpoint per issue #47). A typical first ingest of a medium-sized doc repo takes 30 seconds to a few minutes depending on chunk count + embedding throughput.

**Validation:** the API should now return real results.

```bash
curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "your real query here", "max_results": 5}' \
  https://cf-knowledge-kiln-api.<your-domain>/v1/search | jq
```

---

## Step 9 — Validate retrieval quality

The retrieval eval harness lives at `tests/eval/`. Author a golden set against your corpus and run the harness:

```bash
$EDITOR tests/eval/golden/docs.yaml      # author cases against YOUR docs
make eval                                # opt-in; requires DB + corpus
```

The harness writes `tests/eval/reports/latest.{md,json}`. The Markdown report shows per-case MRR, recall@K, and a "first miss" column to pinpoint regressions. Use it to:

- Establish your bootstrap thresholds (in `tests/eval/test_golden.py`).
- Gate ranking changes (RRF k, FTS weights, HNSW `ef_search`, status boosts) on the harness staying green.

The harness is **opt-in** — it does not run in `make verify` because it requires a seeded corpus. Wire it into your CI as a separate job once thresholds settle.

---

## Cutover checklist

Before you announce the URL to your team:

- [ ] All sources allowlisted in `config/sources.yaml` and ingested at least once.
- [ ] `KILN_AUTH_MODE=bearer` set; token rotated from any dev/staging value.
- [ ] `KILN_TRUST_FORWARDED_FOR` set correctly for your foundation.
- [ ] `/healthz` and `/readyz` both return 200.
- [ ] At least one real query returns expected results.
- [ ] Bearer token shared with intended callers via your normal secrets channel (never email/Slack).
- [ ] The eval harness has at least 5 golden cases and passes its bootstrap thresholds.
- [ ] Feedback widget on `/search` writes to `rag_feedback` (visible in `psql`).
- [ ] Pre-launch checklist in [`docs/security.md`](./security.md) reviewed.

---

## Common gotchas

**"The marketplace plan I picked says `pgvector` but ingestion crashes on `CREATE EXTENSION`."**
The plan ships `pgvector` as available but doesn't install it at provision time. Run `CREATE EXTENSION vector;` once against the bound DB and rerun the worker. Your operator may also need to flip a flag on the broker side.

**"Ingestion can fetch some sources but refuses an HTTPS URL I just added."**
The SSRF guard refuses anything that doesn't pass the host allowlist + IP-range checks. Add the host to `config/sources.yaml` under an `http` source; if it's a redirect chain, every hop must pass the guard.

**"`make eval` keeps failing on Recall@10 after a ranking change."**
Print the `latest.md` report — the "first miss" column tells you exactly which expected hit slipped. If the case is genuinely no longer reachable (because you changed the chunker), update the golden case rather than relaxing the threshold.

**"YAML date frontmatter blew up ingestion."**
Fixed in #94; if you're on an old fork point, rebase or cherry-pick that commit.

**"CodeQL fails on every PR."**
Enable Code Scanning at Repository → Settings → Code security and analysis. The workflow's `analyze` step runs fine but its SARIF upload fails until the feature is on. Tracked upstream as #93.

**"My fork's `manifest.yml` was working, then `cf push` started complaining about routes."**
CF assigns the route from the app name + default domain. After renaming the app, you may need to `cf delete cf-knowledge-kiln-api` (the old name) so the route slot frees up. Check `cf routes` first to avoid clobbering anything you didn't expect.

---

## Where to file feedback

If you walked through this guide and a step was unclear, file an issue against the upstream:

<https://github.com/williamzujkowski/cf-knowledge-kiln/issues>

Tag it `forking-guide` so the next operator's pain compounds the case for a fix.
