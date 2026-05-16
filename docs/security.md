# Security

This is the threat model and hardening reference. The high-level
posture lives in [SECURITY.md](../SECURITY.md); this file goes
deeper.

## Trust boundaries

```text
                                       ┌────────────────┐
client/human/agent ─── HTTP/TLS ─────►│  API process   │── SQL ──► Postgres
                                       └────────┬───────┘
                                                │
                                                │ embed
                                                ▼
                                       model provider (local or HTTPS)
                                                ▲
                                                │ pull
                                       ┌────────┴───────┐
                                       │ Worker process │── git/HTTPS ──► sources
                                       └────────────────┘
```

| Boundary                        | What we trust                                                              |
| ------------------------------- | -------------------------------------------------------------------------- |
| Client → API                    | Bearer token (or mTLS in prod). Auth checked in middleware, not in prompts.|
| API → DB                        | Service-bound credentials. No DB access from any other process.            |
| API/Worker → model provider     | TLS + secret-key auth from env or service binding.                         |
| Worker → source repo            | Allowlist + clone-size limit + file-count limit. SSRF-prevented for HTTP.  |
| **Indexed content → consumer**  | UNTRUSTED. Wrapped with explicit untrusted markers in agent responses.     |

## Untrusted-input handling

This system indexes documentation. Documentation can contain prompt-
injection payloads (intentional or accidental).

- Indexed content is labeled untrusted in every API response that
  returns it.
- Prompt-injection patterns (e.g., "ignore previous instructions",
  "system prompt", "developer message") are detected at ingestion
  and a `prompt_injection_pattern` warning is attached to the chunk.
- The standard `untrusted_content_notice` is included in every agent
  response. Agents that consume context packs must respect it.

Full anti-injection rules live in
[`config/security.example.yaml`](../config/security.example.yaml).

## Secret hygiene

- `gitleaks` runs in pre-commit and CI.
- Secrets are read from env vars or CF service bindings only.
- Secret-bearing config fields never appear in YAML files.
- `bandit` flags insecure-by-default Python patterns.
- `pip-audit` flags known-vulnerable deps.
- SBOM (`syft`) and vulnerability scan (`grype`) run in CI.

## Source ingestion safety

- Sources are allowlisted in `config/sources.yaml`.
- `include` / `exclude` glob patterns limit what files an ingestion run
  may touch.
- File size limit (`KILN_INGEST_MAX_FILE_BYTES`, default 1 MiB).
- HTTP sources (Phase 7+) follow strict SSRF rules: no private IP
  ranges, no link-local, no metadata endpoints.
- Repo clones are shallow and size-capped (Phase 3+).

## What the LLM is allowed to decide

Nothing about access control. Ever. The model never sees a user
identity, never sees an ACL, never gates a response based on prompted
"role". Authentication and authorization live in
`src/cf_knowledge_kiln/api/` middleware.

## Logging

- Query logs (`rag_queries`) record the query, requester, consumer
  type, and retrieved chunk IDs. They do not record secret-bearing
  request headers.
- Model provider keys are redacted from logs at the logger level
  (`structlog` processor).
- Logs do not include indexed content beyond the chunk IDs that were
  retrieved.

## Pre-launch checklist (referenced by Phase 8/9)

- [ ] All external sources allowlisted.
- [ ] Secrets only in env/service bindings.
- [ ] Source URLs sanitized; SSRF prevented.
- [ ] Repository clones size-capped.
- [ ] File size limit enforced.
- [ ] Token limits enforced on agent endpoints.
- [ ] Query logs do not record secret material.
- [ ] CUI/PII assumptions documented.
- [ ] Deprecated docs visibly flagged in retrieval responses.
- [ ] Model licenses/provenance documented in `docs/model-providers.md`.
- [ ] SBOM and Grype scan hooks present and green.
- [ ] Tests cover failure paths.
- [ ] Agent endpoints protected from prompt-injection patterns.
- [ ] AI consumers warned not to execute retrieved text.
