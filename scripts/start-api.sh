#!/usr/bin/env bash
# Start the API. CF passes $PORT; locally we default to 8080.
set -euo pipefail

PORT="${PORT:-${KILN_HTTP_PORT:-8080}}"

exec python -m uvicorn cf_knowledge_kiln.api.app:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --workers "${KILN_WEB_WORKERS:-2}" \
  --access-log
