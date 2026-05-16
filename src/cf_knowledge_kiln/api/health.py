"""Liveness, readiness, and version endpoints.

- ``/healthz`` is the cheap liveness probe. It must not touch the DB.
- ``/readyz`` is the readiness probe. From Phase 2 onward it verifies
  the Postgres connection; for now it returns ``ready: true`` because
  the API has no external dependencies wired in.
- ``/version`` reports the package version.

These shapes are stable. CF health checks point at ``/healthz``.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel

from cf_knowledge_kiln import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness response shape."""

    status: str
    service: str


class ReadyResponse(BaseModel):
    """Readiness response shape.

    ``checks`` maps each dependency name to ``ok`` or ``failing``. An
    empty map means the service has no external dependencies wired in
    yet.
    """

    status: str
    checks: dict[str, str]


class VersionResponse(BaseModel):
    """Version response shape."""

    version: str


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
)
async def healthz() -> HealthResponse:
    """Return liveness. Cheap, no I/O."""
    return HealthResponse(status="ok", service="cf-knowledge-kiln")


@router.get(
    "/readyz",
    response_model=ReadyResponse,
    status_code=status.HTTP_200_OK,
    summary="Readiness probe",
)
async def readyz() -> ReadyResponse:
    """Return readiness.

    Phase 2 will populate ``checks`` with a Postgres ping. Until then,
    the endpoint exists so CF / load balancers can be wired correctly
    from day one.
    """
    return ReadyResponse(status="ready", checks={})


@router.get(
    "/version",
    response_model=VersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Service version",
)
async def version() -> VersionResponse:
    """Return the running package version."""
    return VersionResponse(version=__version__)
