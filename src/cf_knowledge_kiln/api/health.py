"""Liveness, readiness, and version endpoints.

- ``/healthz`` is the cheap liveness probe. It must not touch the DB.
- ``/readyz`` reports per-dependency status. From Phase 2 it pings
  Postgres and surfaces ``postgres: ok | failing``.
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

CheckValue = Literal["ok", "failing"]
ReadyStatus = Literal["ready", "degraded"]


class HealthResponse(BaseModel):
    status: str
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
    """Return readiness with a Postgres ping.

    ``postgres`` is ``failing`` when the pool is not configured (no URL,
    no VCAP binding) or when the ``SELECT 1`` round-trip fails. When any
    check fails the HTTP status code is **503** so CF/gorouter and
    upstream load balancers route traffic away from the instance; the
    body still carries the per-check details.

    ``/healthz`` (liveness) stays 200 unconditionally — a failing
    liveness causes a restart loop, which isn't what a missing
    Postgres warrants.
    """
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:
        postgres: CheckValue = "failing"
    else:
        postgres = "ok" if await db.ping() else "failing"
    checks: dict[str, CheckValue] = {"postgres": postgres}
    overall: ReadyStatus = "ready" if all(v == "ok" for v in checks.values()) else "degraded"
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
