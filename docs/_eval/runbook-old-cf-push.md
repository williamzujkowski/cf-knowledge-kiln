---
title: "DEPRECATED: cf push the kiln API and worker"
status: deprecated
owner: platform
doc_type: runbook
sensitivity: internal
last_reviewed: 2024-02-01
tags: [deprecated, runbook, cloud-foundry, deployment]
---

## DEPRECATED runbook — cf push the kiln API and worker

**This runbook is deprecated.** It documents the pre-manifest, ad-hoc
`cf push` flow we used before the `manifest.yml` two-app shape
landed. Following the steps below produces a single instance with no
worker and a random route — neither matches the current production
topology. Use the current runbook instead.

## Steps (DEPRECATED)

```bash
cf push cf-knowledge-kiln-api \
  --buildpack python_buildpack \
  --random-route \
  --memory 512M \
  -c "python -m uvicorn cf_knowledge_kiln.api.app:app --host 0.0.0.0 --port \$PORT"
```

The worker process was managed out-of-band via a sibling
`cf push cf-knowledge-kiln-worker` with `-c "python -m cf_knowledge_kiln.ingestion serve-worker"`.

## Why deprecated

- No `manifest.yml` means no reproducible deploys across spaces.
- `--random-route` collides with the canonical
  `cf-knowledge-kiln-api.<system-domain>` route once the manifest is
  also pushed.
- The worker app needs `no-route: true`; that flag isn't passable on
  the command line and silently became `false` in this flow.

The current runbook (filed under `operations/`) is the only supported
path. This page is preserved for archaeology and to anchor
deprecation tests against a known-deprecated chunk in the corpus.
