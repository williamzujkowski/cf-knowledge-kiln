---
title: Requeue a stuck ingest job
status: active
owner: platform
doc_type: sop
sensitivity: internal
last_reviewed: 2026-04-30
tags: [sop, ingestion, ops, queue]
---

## Requeue a stuck ingest job

An ingest job is "stuck" when its `ingestion_runs` row has
`state = running` and `started_at < now() - interval '5 minutes'`. The
worker's `recover_stale_running` recovery flips these back to `queued`
on every boot; this SOP is the manual path when the worker can't be
restarted (mid-deploy, blocked maintenance window).

## Detect

```sql
SELECT id, source_name, started_at, now() - started_at AS stuck_for
FROM ingestion_runs
WHERE state = 'running'
  AND started_at < now() - interval '5 minutes'
ORDER BY started_at;
```

## Decide

If the same `source_name` shows up repeatedly, the source itself is
probably failing (clone timeout, malformed frontmatter blowing up the
chunker). Investigate before requeuing — requeuing the same broken
source loops the worker.

## Requeue

```sql
UPDATE ingestion_runs
SET state = 'queued', started_at = NULL, completed_at = NULL
WHERE id = '<stuck-id>';
```

Then `cf restart cf-knowledge-kiln-worker` so the boot loop picks up
the new queued row.

## Notes

This SOP never deletes the row — the audit trail (start time, error,
chunks_seen) is the evidence stream for `requires_human_review`
calibration and post-incident review. See ADR-0009 for the broader
"audit before delete" stance.
