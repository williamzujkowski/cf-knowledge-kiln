#!/usr/bin/env bash
# Phase 7 smoke test for a deployed cf-knowledge-kiln-api.
#
# Verifies the four endpoints an operator needs to trust before
# a release goes live:
#
#   1. GET /healthz   — process liveness (must always 200)
#   2. GET /version   — wrong-version detection
#   3. GET /readyz    — DB binding actually works (200 or 503; we
#                       check the body shape)
#   4. POST /v1/search — the round-trip that exercises auth,
#                        retrieval, telemetry, and DB writes
#
# Designed to run against:
#   - A fresh `cf push` (default URL pattern below).
#   - A local `make run` (KILN_URL=http://localhost:8080).
#   - An apps.internal route via curl from an app in the same CF org.
#
# Usage:
#   KILN_URL=https://cf-knowledge-kiln-api.example.com \
#   KILN_BEARER_TOKEN=<secret> \
#   scripts/smoke-test.sh
#
# Or with a query of your own:
#   KILN_URL=... KILN_BEARER_TOKEN=... ./scripts/smoke-test.sh "your query"
#
# Exit codes:
#   0 — every check passed.
#   1 — at least one check failed (non-2xx on a 200-expected endpoint
#       OR a malformed response body).
#   2 — usage / missing config.

set -euo pipefail

KILN_URL="${KILN_URL:-}"
KILN_BEARER_TOKEN="${KILN_BEARER_TOKEN:-}"
KILN_AUTH_MODE="${KILN_AUTH_MODE:-bearer}"
QUERY="${1:-cf knowledge kiln smoke}"

if [[ -z "${KILN_URL}" ]]; then
  echo "ERROR: set KILN_URL (e.g. https://cf-knowledge-kiln-api.example.com)" >&2
  exit 2
fi

# Strip trailing slash for clean URL construction.
KILN_URL="${KILN_URL%/}"

# Build curl auth arg only when bearer mode is on. The smoke test for
# a dev (`none`-mode) deployment leaves the header off entirely.
AUTH_ARGS=()
if [[ "${KILN_AUTH_MODE}" == "bearer" ]]; then
  if [[ -z "${KILN_BEARER_TOKEN}" ]]; then
    echo "ERROR: KILN_AUTH_MODE=bearer requires KILN_BEARER_TOKEN" >&2
    exit 2
  fi
  AUTH_ARGS=(-H "Authorization: Bearer ${KILN_BEARER_TOKEN}")
fi

pass=0
fail=0

check_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "${actual}" == "${expected}" ]]; then
    printf "  ✓ %-30s %s\n" "${label}" "${actual}"
    pass=$((pass + 1))
  else
    printf "  ✗ %-30s expected=%s actual=%s\n" "${label}" "${expected}" "${actual}" >&2
    fail=$((fail + 1))
  fi
}

check_contains() {
  local label="$1" needle="$2" body="$3"
  if [[ "${body}" == *"${needle}"* ]]; then
    printf "  ✓ %-30s body contains %q\n" "${label}" "${needle}"
    pass=$((pass + 1))
  else
    printf "  ✗ %-30s body missing %q\n  body=%.200s\n" "${label}" "${needle}" "${body}" >&2
    fail=$((fail + 1))
  fi
}

echo "smoke-test cf-knowledge-kiln @ ${KILN_URL}"
echo "  auth: ${KILN_AUTH_MODE}"
echo

# 1. /healthz — must always 200, regardless of auth.
echo "[1/4] GET /healthz"
status=$(curl -fsS -o /tmp/kiln-healthz.json -w "%{http_code}" "${KILN_URL}/healthz")
check_eq "status" "200" "${status}"
check_contains "body.status" "ok" "$(cat /tmp/kiln-healthz.json)"
echo

# 2. /version — must always 200.
echo "[2/4] GET /version"
status=$(curl -fsS -o /tmp/kiln-version.json -w "%{http_code}" "${KILN_URL}/version")
check_eq "status" "200" "${status}"
check_contains "body.version" "version" "$(cat /tmp/kiln-version.json)"
echo

# 3. /readyz — 200 when ready, 503 when degraded. Both are acceptable;
#    we check the body shape to distinguish "expected degraded" (e.g.,
#    no DB bound in dev) from "actually broken" (missing checks block).
echo "[3/4] GET /readyz"
status=$(curl -sS -o /tmp/kiln-readyz.json -w "%{http_code}" "${KILN_URL}/readyz")
if [[ "${status}" == "200" || "${status}" == "503" ]]; then
  printf "  ✓ %-30s %s\n" "status (200 or 503)" "${status}"
  pass=$((pass + 1))
else
  printf "  ✗ %-30s unexpected status %s\n" "status (200 or 503)" "${status}" >&2
  fail=$((fail + 1))
fi
check_contains "body.checks.postgres" "postgres" "$(cat /tmp/kiln-readyz.json)"
echo

# 4. /v1/search — the real round-trip. Asserts auth fired (not 401),
#    Pydantic accepted the body (not 422), and the response shape is
#    a SearchResponse. We don't assert non-empty results because a
#    freshly-pushed app may have an empty index.
echo "[4/4] POST /v1/search  query=$(printf '%q' "${QUERY}")"
status=$(curl -sS -o /tmp/kiln-search.json -w "%{http_code}" \
  "${AUTH_ARGS[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"${QUERY}\", \"max_results\": 3}" \
  "${KILN_URL}/v1/search")
check_eq "status" "200" "${status}"
check_contains "body.query" "${QUERY}" "$(cat /tmp/kiln-search.json)"
check_contains "body.results" "results" "$(cat /tmp/kiln-search.json)"
echo

echo "smoke-test summary: ${pass} pass / ${fail} fail"
if [[ "${fail}" -gt 0 ]]; then
  echo "smoke test FAILED — inspect /tmp/kiln-{healthz,version,readyz,search}.json" >&2
  exit 1
fi
echo "smoke test PASSED"
