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
- **SBOM + vulnerability scan**: every push runs `anchore/sbom-action`
  (syft) to emit an SPDX JSON SBOM and `anchore/scan-action` (grype)
  to scan it against the CVE feed. Build fails on **HIGH / CRITICAL**
  findings (cutoff is `severity-cutoff: high` in the workflow); the
  SBOM is uploaded as a 90-day-retention artifact (`cf-knowledge-kiln-sbom`)
  so an operator can audit the dep tree of any merged commit. Locally:
  `make sbom && make scan` (requires `syft` + `grype` on $PATH).

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

State as of the Phase 9 public-readiness review (#34):

- [x] All external sources allowlisted. (`SourceAllowlist`; #27)
- [x] Secrets only in env/service bindings. (`Settings` + `VCAP_SERVICES` resolution; never in YAML config)
- [x] Source URLs sanitized; SSRF prevented. (#80 host allowlist + IP-range checks + IPv6 transition blocks; #81 DNS pinning closes the TOCTOU window)
- [x] Repository clones size-capped. (`KILN_INGEST_MAX_REPO_BYTES`)
- [x] File size limit enforced. (`KILN_INGEST_MAX_FILE_BYTES`)
- [x] Token limits enforced on agent endpoints. (`max_tokens` on `/v1/agent/context-pack`)
- [x] Query logs do not record secret material. (`db.redact_dsn` for connection strings; query bodies log at INFO without secrets)
- [x] CUI/PII assumptions documented. (this file)
- [x] Deprecated docs visibly flagged in retrieval responses. (`status` warnings on result cards + agent packs)
- [x] Model licenses/provenance documented in `docs/model-providers.md`. (US-origin allowlist; adversary-origin weights refused at load time)
- [x] SBOM and Grype scan hooks present and green. (`make sbom` + `make scan`; CI gate severity-cutoff `high`; #28)
- [x] Tests cover failure paths. (454 unit + 101 integration as of #94; retrieval eval harness at `tests/eval/` with `make eval`)
- [x] Agent endpoints protected from prompt-injection patterns. (ingest-time scanner stamps `has_prompt_injection`; retrieval emits the warning O(1) per chunk)
- [x] AI consumers warned not to execute retrieved text. (`untrusted_content_notice` always present in context-pack response)
- [x] Per-IP rate limit on `/v1/search`, `/v1/agent/context-pack`, and `/feedback`. (#79; in-process token-bucket with LRU bucket cap; single-instance only — horizontal scale needs a shared backend)
