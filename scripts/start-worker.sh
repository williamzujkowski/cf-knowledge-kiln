#!/usr/bin/env bash
# Start the ingestion worker daemon.
#
# This script is the manifest.yml `command:` for the worker app. CF
# launches it from /home/vcap/app after staging. The daemon polls
# `ingestion_jobs` and drains queued sources via the configured
# embedding provider — it must be a long-running process for CF's
# process health-check to mark the instance Started.
#
# `serve-worker` is the daemon subcommand on the ingestion CLI. The
# old Phase-1 shim (``python -m cf_knowledge_kiln.ingestion.worker``)
# imported the worker module and exited 0 — CF interpreted that as a
# crashed start and crash-looped the app. See #240 for the writeup.
#
# `--config` defaults to ``$KILN_SOURCE_ALLOWLIST_PATH`` (set by the
# manifest's env block) and falls back to ``config/sources.yaml``.
# Honoring the env var here keeps the worker, the API, and `make
# ingest` aligned on a single source allowlist (#243).
set -euo pipefail

exec python -m cf_knowledge_kiln.ingestion \
  --config "${KILN_SOURCE_ALLOWLIST_PATH:-config/sources.yaml}" \
  serve-worker
