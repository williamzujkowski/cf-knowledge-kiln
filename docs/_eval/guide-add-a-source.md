---
title: "Guide: add a new ingestion source"
status: active
owner: platform
doc_type: guide
sensitivity: internal
last_reviewed: 2026-05-03
tags: [guide, ingestion, sources, configuration]
---

## Guide: add a new ingestion source

Sources are configured in `config/sources.yaml` (gitignored;
`config/sources.example.yaml` is the template). The kiln refuses to
ingest a source not on the allowlist, so adding one is a deliberate
config change, not a runtime call.

## Source shape

```yaml
- name: my-team-runbooks
  type: git
  repo: my-org/my-team-runbooks
  branch: main
  include: ["runbooks/**/*.md", "AGENTS.md"]
  exclude: ["docs/generated/**"]
  status: active
  authority: standard
  default_owner: my-team
  default_sensitivity: internal
  last_reviewed_required: false
```

## Trust + status defaults

`status` and `authority` set the floor for every chunk this source
produces. A source marked `status: draft` makes every chunk
queryable only when the user opts in to drafts via the status
pill in the UI (or `filters.status: ['draft']` in the API).

## Local sources

`type: local` reads from a filesystem path. Useful for the eval
suite (`docs/` and `docs/_eval/` are ingested via `LocalSource`)
and for operators who maintain documentation on a mounted volume.
Local sources have no `commit_sha` in their citations.

## Verify

After editing `config/sources.yaml`, run `make ingest` and watch the
log for `ingestion_runs` rows reaching `state = complete` with a
non-zero `chunks_seen`. If a source produces zero chunks, the
include/exclude patterns aren't matching the repo's real layout.
