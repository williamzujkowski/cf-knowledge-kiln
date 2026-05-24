# Model providers

This document is the **active allowlist**. PRs that touch `config/models.yaml`
get reviewed against this file. See [ADR-0005](./adr/0005-model-provider-abstraction.md)
for the architectural rationale.

## Allowed for MVP

| Role        | Name                              | Provider/Origin    | License                      | Status (MVP) |
| ----------- | --------------------------------- | ------------------ | ---------------------------- | ------------ |
| Embedding   | `nomic-embed-text-v1.5`           | Nomic AI (US)      | Apache 2.0                   | Active (MVP per [ADR-0002](./adr/0002-postgres-pgvector.md) / [ADR-0008](./adr/0008-pgvector-mvp-critical.md)) |
| Generator   | `microsoft/Phi-4-mini-instruct`   | Microsoft (US)     | Microsoft Research License   | Disabled until Phase 5 lands `/v1/answer` |

## Possible additions, subject to provenance review

- Small Llama-family generators (Meta, US).
- Other US-origin sentence-transformer embeddings with a permissive
  license. `Snowflake/snowflake-arctic-embed-m` (Snowflake, US,
  Apache 2.0, 768-dim) is the next likely candidate — it drops in via
  `provider: local-sentence-transformers` with no code change.

## Excluded for MVP

The MVP avoids China-origin model families. This is a plan-level
constraint, captured here so PRs do not have to re-derive it:

- `Qwen` family
- `DeepSeek` family
- `BAAI/BGE` family

Removing a model from this list requires an ADR.

## Adding a new model

1. Confirm provenance (publisher org country / jurisdiction).
2. Confirm license is compatible (MIT, Apache 2.0, BSD, MSR-license,
   Llama-community-license — check terms).
3. Add a row to the **Allowed for MVP** table above.
4. Update `config/models.example.yaml`.
5. If swapping the active embedding model, follow the checklist below.

## Swapping the active embedding model

The adapter (`ingestion/embedding/local.py`) is a generic
sentence-transformers wrapper — a swap is a `config/models.yaml`
change, not a code change. The four things that are *per-model* and
must be set in config:

- **`name`** — the full HuggingFace `org/model` id (a bare name will
  not resolve).
- **`dimensions`** — the model's actual output width. The adapter
  checks the first batch against this and raises loudly on a
  mismatch, so a wrong value fails fast instead of writing
  wrong-width vectors.
- **`trust_remote_code`** — `true` only for models that ship custom
  modeling code (Nomic Embed v1.5 → `nomic-bert-2048`). Defaults to
  `false`; it executes code downloaded from the model hub, so it is
  always an explicit per-model opt-in. A model that does not need it
  must leave it `false`.
- **`normalize`** (provider arg; default `true`) — L2-normalizes
  output so cosine ranking stays correct. Leave `true` unless the
  model is documented to require raw vectors.

### Model-family text prefixes (#204)

Some embedding models were trained with explicit role prefixes and
collapse into the 0.1–0.4 cosine-similarity band when called without
them. The adapter applies the right prefix in `embed_documents` /
`embed_query` based on the model name:

| Family pattern                          | Passage prefix          | Query prefix          |
| --------------------------------------- | ----------------------- | --------------------- |
| `intfloat/e5-*` (incl. multilingual-e5) | `` `passage: ` ``       | `` `query: ` ``       |
| `nomic-ai/nomic-embed-text-v1*`         | `` `search_document: `` | `` `search_query: ``  |

Models not matching any family pattern pass through unchanged
(`Snowflake/snowflake-arctic-embed-m`, `BAAI/bge-m3`, OpenAI hosted
models, etc.). If you swap in a new family that needs prefixes, add
the pattern to `_MODEL_PREFIXES` in
`src/cf_knowledge_kiln/ingestion/embedding/local.py` — the table is
the only place the mapping lives.

The `embed()` method on the adapter is preserved as a raw, no-prefix
path for the startup health probe and back-compat. New code should
call `embed_documents` (for chunks being indexed) or `embed_query`
(for a user-supplied query).

**Migration note: existing corpora need re-ingestion.** Chunks
embedded before #204 landed were encoded WITHOUT the prefix; new
queries (after #204) are encoded WITH the prefix. The two live in
different semantic spaces, so old chunks rank poorly against new
queries on the same e5/Nomic model. Re-embed the corpus to get the
calibrated cosine range back:

```sql
-- The simplest path: drop the embedding rows; the worker re-embeds
-- on next ingestion pass. Chunks themselves stay intact (FK is the
-- chunk row, not the embedding).
TRUNCATE chunk_embeddings;
```

```bash
make ingest   # worker re-embeds every chunk against the prefix-aware provider
```

For larger corpora where TRUNCATE-and-re-ingest is too slow, scope
the delete to one provider/model with a `WHERE provider = '...' AND
model = '...'` clause and let the worker backfill.

Reindex requirement: `chunk_embeddings.dimensions` is per-row, so
rows from different models coexist — but the HNSW index created in
migration `0001` is a *partial* index bound to one dimension
(`ix_chunk_embeddings_hnsw_768`). Swapping to a model with a
different width needs a follow-up migration adding a partial index
for the new dimension, plus a re-embed of the corpus.

## Why the China-origin exclusion?

The plan calls it as a hard constraint for the MVP. It is not a
permanent policy; it is a constraint scoped to this MVP. The
implementation enforces it through review of this allowlist and of
`config/models.yaml`, not through a runtime block — model files are
fetched out-of-band, so a runtime block would be a false sense of
control.
