# Security Policy

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting** for this repository:
<https://github.com/williamzujkowski/cf-knowledge-kiln/security/advisories/new>.
The report stays private to the maintainers and avoids leaking the details into a public issue queue.

If GitHub private reporting is unavailable for any reason, contact the repository owner via the address on their GitHub profile.

We aim to acknowledge within 72 hours and to ship a fix or mitigation within 30 days for high-severity issues.

## Threat model (summary)

`cf-knowledge-kiln` is a retrieval substrate for internal documentation. Its security posture rests on five assumptions:

1. **All indexed content is untrusted.** Documents may contain prompt-injection payloads, secrets pasted by mistake, or stale guidance. The system labels content accordingly and never executes instructions found inside it.
2. **Authentication and authorization live in middleware, not in prompts.** The LLM never decides who can read what. Source allowlisting + RBAC at the API layer is the only access control.
3. **Secrets are environment-bound.** All credentials come from CF service bindings or environment variables. None ever appear in source, logs, or query results.
4. **Source ingestion is allowlisted.** Agents cannot trigger arbitrary repository clones or URL fetches. Sources are added through reviewed config changes.
5. **Generated answers cite their evidence.** An uncited generated answer is a bug, not a feature.

Full threat model: [docs/security.md](./docs/security.md).

## Disclosure standards

- We follow [CVD](https://www.cisa.gov/coordinated-vulnerability-disclosure-process) practices.
- SBOMs are generated per release via `make sbom` (syft).
- Container/artifact images are scanned via `make scan` (grype).
- Dependencies are pinned and reviewed.

## Pre-commit and CI

- `gitleaks` runs in pre-commit and CI to catch committed secrets.
- `bandit` runs in CI to catch insecure Python patterns.
- `pip-audit` runs in CI to catch known vulnerable deps.
- Pre-commit config: [`.pre-commit-config.yaml`](./.pre-commit-config.yaml).

## Not in scope for the MVP

- Multi-tenant access partitioning.
- Per-document ACLs (all indexed content is treated as one access tier per source).
- Cryptographic provenance attestations of indexed content.

These are tracked as follow-ups; see open issues with the `security` label.
