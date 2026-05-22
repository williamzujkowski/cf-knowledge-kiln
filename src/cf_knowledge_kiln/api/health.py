"""Liveness, readiness, and version endpoints.

- ``/healthz`` is the cheap liveness probe. It must not touch the DB.
- ``/readyz`` reports per-dependency status. It pings Postgres
  (``postgres: ok | failing``) and reports the embedding provider's
  startup-probe result (``embedding: ok | failing | not_configured``).
- ``/version`` reports the package version.

CF health checks point at ``/healthz``; readiness consumers (load
balancers, ops dashboards) call ``/readyz``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from cf_knowledge_kiln import __version__
from cf_knowledge_kiln.db import Database

router = APIRouter(tags=["health"])

CheckValue = Literal["ok", "failing", "not_configured"]
"""Per-dependency check value.

``not_configured`` is informational — it means the dependency is
deliberately absent (e.g. no embedding provider → FTS-only retrieval),
which is a valid mode and does NOT degrade readiness. Only ``failing``
rolls the overall status to ``degraded``."""

ReadyStatus = Literal["ready", "degraded"]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ReadyResponse(BaseModel):
    """Readiness response: per-dependency check map + roll-up status."""

    status: ReadyStatus
    checks: dict[str, CheckValue]


class VersionResponse(BaseModel):
    version: str


@router.get(
    "/healthz",
    operation_id="healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
)
async def healthz() -> HealthResponse:
    """Return liveness. Cheap, no I/O."""
    return HealthResponse(status="ok", service="cf-knowledge-kiln")


@router.get(
    "/readyz",
    operation_id="readyz",
    response_model=ReadyResponse,
    summary="Readiness probe",
    responses={
        status.HTTP_200_OK: {"description": "All dependency checks pass."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "At least one dependency check is failing.",
            "model": ReadyResponse,
        },
    },
)
async def readyz(request: Request, response: Response) -> ReadyResponse:
    """Return readiness: a Postgres ping + the embedding-provider probe.

    ``postgres`` is ``failing`` when the pool is not configured (no URL,
    no VCAP binding) or when the ``SELECT 1`` round-trip fails.

    ``embedding`` reflects the one-shot provider probe run at startup
    (#176): ``ok`` (probe embed succeeded), ``failing`` (probe raised —
    e.g. a bad embedding base-URL builds a provider object that only
    fails on use), or ``not_configured`` (no embedding config; FTS-only
    retrieval, a valid mode).

    Only a ``failing`` check rolls the status to ``degraded``;
    ``not_configured`` does not. When degraded the HTTP status is
    **503** so CF/gorouter and upstream load balancers route traffic
    away from the instance; the body still carries the per-check map.

    ``/healthz`` (liveness) stays 200 unconditionally — a failing
    liveness causes a restart loop, which isn't what a missing
    dependency warrants.
    """
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        postgres: CheckValue = "failing"
    else:
        postgres = "ok" if await db.ping() else "failing"
    embedding: CheckValue = getattr(request.app.state, "embedding_status", "not_configured")
    checks: dict[str, CheckValue] = {"postgres": postgres, "embedding": embedding}
    overall: ReadyStatus = "degraded" if any(v == "failing" for v in checks.values()) else "ready"
    if overall == "degraded":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status=overall, checks=checks)


@router.get(
    "/version",
    operation_id="version",
    response_model=VersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Service version",
)
async def version() -> VersionResponse:
    """Return the running package version."""
    return VersionResponse(version=__version__)
