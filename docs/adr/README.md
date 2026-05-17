# Architectural Decision Records

Plain-Markdown ADRs with YAML frontmatter, mirroring the [homelab-iac
proposal pattern](https://github.com/williamzujkowski/homelab-iac/tree/main/docs/proposals).
Status values: `proposed | accepted | rejected | superseded`.

Numbering is monotonic. When superseding an earlier ADR, set the new
ADR's `supersedes` field and the older ADR's `superseded_by` field.

| #     | Title                                                  | Status     |
| ----- | ------------------------------------------------------ | ---------- |
| 0001  | Use Python for the MVP                                 | accepted   |
| 0002  | Postgres + pgvector as the vector store                | accepted (reinstated by 0008) |
| 0003  | OpenAPI 3.1 + separate human and agent shapes          | accepted   |
| 0004  | Cloud Foundry process model                            | accepted   |
| 0005  | Model provider abstraction + provenance rules          | accepted   |
| 0007  | FTS-first retrieval; embeddings deferred to Phase 5.5  | superseded by 0008 |
| 0008  | Embeddings are MVP-critical; pgvector back in Phase 2  | accepted   |
| 0009  | Hybrid retrieval — RRF over pgvector + Postgres FTS    | accepted   |
