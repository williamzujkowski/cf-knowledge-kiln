#!/usr/bin/env bash
# Start the ingestion worker. Phase 3 wires the real entry point;
# Phase 1 sleeps so CF marks the process as healthy.
set -euo pipefail

if python -c "import cf_knowledge_kiln.ingestion.worker" 2>/dev/null; then
  exec python -m cf_knowledge_kiln.ingestion.worker
fi

echo "cf-knowledge-kiln-worker: ingestion worker not yet implemented (Phase 3)" >&2
echo "cf-knowledge-kiln-worker: sleeping; restart this app once Phase 3 lands." >&2
exec sleep infinity
