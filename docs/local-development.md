# Local development

End-to-end recipe for standing up cf-knowledge-kiln on a laptop, against
a Docker-hosted pgvector Postgres. Built from #199 — the README's older
Quick Start jumped from `make bootstrap` to `make cf-push` and left new
operators reverse-engineering the rest from `scripts/start-*.sh`.

For Cloud Foundry deployment, see
[docs/deployment-cloud-foundry.md](./deployment-cloud-foundry.md). For
the env-var reference, see [docs/configuration.md](./configuration.md).

## Prerequisites

- `python3.12` on `$PATH`
- `docker` for the pgvector container
- A Unix-ish shell. Commands below assume `bash`.

## One-shot bootstrap

Copy-paste the whole block. It's idempotent — re-running is safe.

```bash
# --- pgvector container -----------------------------------------------
docker run -d --name kiln-pg \
  -e POSTGRES_PASSWORD=kiln \
  -e POSTGRES_USER=kiln \
  -e POSTGRES_DB=kiln \
  -p 5432:5432 \
  pgvector/pgvector:pg16

# --- repo + venv -------------------------------------------------------
# (you're presumably already in cf-knowledge-kiln/)
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,db,ingestion,embeddings]"

# --- required env vars -------------------------------------------------
# In a `.env` file or `direnv` config — the snippet below is `export`s
# for an interactive shell. ``KILN_ENV=development`` is required for
# ``KILN_AUTH_MODE=none``; auth refuses ``none`` in production by design.
export KILN_DATABASE_URL="postgresql+asyncpg://kiln:kiln@localhost:5432/kiln"  # pragma: allowlist secret
export KILN_ENV=development
export KILN_AUTH_MODE=none

# --- schema + config ---------------------------------------------------
.venv/bin/alembic upgrade head
cp config/models.example.yaml  config/models.yaml         # edit if you want a different model
cp config/sources.example.yaml config/sources.local.yaml  # gitignored copy you can edit freely

# --- pre-warm the embedding model (avoids #198 startup-probe timeout) --
# Skip this if your models.yaml uses the mock or openai-compatible provider.
.venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
  SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', \
    trust_remote_code=True, device='cpu').encode(['x'])"

# --- validate your sources file before the worker runs ----------------
.venv/bin/python -m cf_knowledge_kiln.ingestion validate \
  --config config/sources.local.yaml
```

## Run it (two processes)

In one terminal — the API:

```bash
.venv/bin/python -m uvicorn cf_knowledge_kiln.api.app:app \
  --host 127.0.0.1 --port 8000
```

In another terminal — the ingestion worker:

```bash
.venv/bin/python -m cf_knowledge_kiln.ingestion serve-worker \
  --config config/sources.local.yaml
```

The worker also has a `python -m cf_knowledge_kiln.ingestion.worker`
shorthand that uses the default config path. The `serve-worker` form
above is what `scripts/start-worker.sh` calls in production.

## Verify

```bash
curl -sf http://localhost:8000/healthz
# {"status":"ok","service":"cf-knowledge-kiln"}

curl -sf http://localhost:8000/readyz
# {"status":"ready","checks":{"postgres":"ok","embedding":"ok"}}
```

If `embedding: failing` appears, the startup probe tripped — likely a
cold HuggingFace download. Either pre-warm again (the snippet above)
or bump `KILN_EMBEDDING_PROBE_TIMEOUT_SECONDS` and restart. See
[docs/configuration.md](./configuration.md#env-var-reference) for the
full env-var list and #198 for the background.

Hit the human UI at <http://localhost:8000/search>; hit the JSON
search at:

```bash
curl -sX POST http://localhost:8000/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"YOUR QUERY","max_results":5}' | jq
```

## Why each step

| Step | Why |
| ---- | --- |
| pgvector container | The retrieval CTE needs the `vector` extension; vanilla Postgres won't do. The official `pgvector/pgvector:pg16` image has it pre-installed. |
| `.venv/bin/pip install -e ".[dev,db,ingestion,embeddings]"` | `dev` for ruff/mypy/pytest, `db` for asyncpg+pgvector+alembic, `ingestion` for markdown+tiktoken, `embeddings` for sentence-transformers. Drop `embeddings` if you're using the OpenAI-compatible or mock provider — saves a ~500 MB torch install. |
| `KILN_ENV=development` | Auth refuses `KILN_AUTH_MODE=none` outside development as a Trap #16 guard against accidentally shipping an unauthenticated API. |
| `KILN_AUTH_MODE=none` | No bearer/mTLS needed for local poking. Use `bearer` + `KILN_BEARER_TOKEN` for anything you'd expose. |
| `alembic upgrade head` | Apply migrations to the empty pgvector DB. Re-running is a no-op. |
| `cp config/models.example.yaml config/models.yaml` | The runtime expects `config/models.yaml` (gitignored); the `.example` file is the committed template. |
| `cp config/sources.example.yaml config/sources.local.yaml` | `.local.yaml` is gitignored (#198 follow-up) so you can edit your source list freely without staging. |
| Pre-warm the model | The startup probe times out at 90 s by default; a cold HuggingFace pull on first start can exceed that and pin `/readyz` to `embedding: failing`. Pre-warming sidesteps the timed path. |
| `ingestion validate` | Catches a malformed sources file before the worker runs. The worker would otherwise raise at startup with a deeper stack trace. |

## Common stumbling blocks

- **Trap #16: `KILN_AUTH_MODE=none` refused.** Set `KILN_ENV=development`. The refusal is intentional — the auth module raises at startup so an unauthenticated mode can't ship to prod by accident.
- **`/readyz` says `postgres: failing`.** Most likely `KILN_DATABASE_URL` is wrong or the container isn't listening yet. `docker logs kiln-pg` and confirm the URL uses `+asyncpg`.
- **`/v1/search` returns 503 with `embedding_provider: not_configured`.** No `config/models.yaml` is present. Copy the example, then restart the API process.
- **First `/v1/search` is slow then OK.** The provider lazy-loads the model on first call; the pre-warm above avoids it. After the first call, the model stays in memory for the life of the process.

## Re-embedding after a model swap (#224)

If you change the active embedding model in `config/models.yaml`
(e.g. swap `nomic-embed-text-v1.5` for `intfloat/e5-small-v2`) or land
a prefix-handling fix like #204, existing chunk embeddings need to be
regenerated against the new shape:

```bash
make reembed-dry-run   # preview: "would re-embed N chunks via <provider>/<model>"
make reembed           # actually re-embed
```

The helper walks every row in `document_chunks`, calls the active
provider's `embed_documents`, and upserts into `chunk_embeddings`.
Partial failures are non-fatal: surviving batches persist, the
report shows `<embedded>/<failed>/<total>`, and the failed batches'
forensics land in the log at WARNING.

See [docs/model-providers.md](./model-providers.md#model-family-text-prefixes-204)
for the full background.

## Tearing it down

```bash
docker stop kiln-pg && docker rm kiln-pg
deactivate            # if you sourced .venv/bin/activate
```

The HuggingFace cache lives at `~/.cache/huggingface/`; remove it
with `rm -rf ~/.cache/huggingface/` if you want a true cold rebuild.
