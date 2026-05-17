"""Phase 5 route stubs — register the contract surface, return 501.

The hand-authored `openapi/openapi.yaml` has declared
`/v1/search` and `/v1/agent/context-pack` since Phase 1 (ADR-0003).
Phase 5 will implement them; until then this module returns
HTTP 501 so:

* Clients can test their 501-handling against the real server
  instead of the documented-but-absent contract.
* The OpenAPI drift test (tests/unit/test_openapi_drift.py) can
  compare paths and operationIds without needing schema parity for
  the not-yet-implemented bodies.

Per the Phase 5 design doc, when implementation lands, this module
gets replaced with the real routes that delegate into
`HybridRetriever`. The OpenAPI operationIds stay stable across
that change so consumers don't have to re-bind.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status

router = APIRouter(tags=["search"])


_NOT_YET = (
    "/v1/search and /v1/agent/* are declared in the OpenAPI contract "
    "but the Phase 5 implementation has not landed; see issue #4."
)


@router.post(
    "/v1/search",
    operation_id="humanSearch",
    summary="Human search (Phase 5+)",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    responses={
        # The 200 entry keeps the forward-looking contract in sync with
        # openapi/openapi.yaml; the runtime returns 501 until Phase 5.
        status.HTTP_200_OK: {"description": "Search results (Phase 5)."},
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "Not implemented yet (Phase 5).",
        },
    },
)
async def human_search(_body: Annotated[dict[str, Any], Body(default_factory=dict)]) -> None:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_YET)


@router.post(
    "/v1/agent/context-pack",
    operation_id="agentContextPack",
    summary="Build an agent context pack (Phase 5+)",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    tags=["agent"],
    responses={
        status.HTTP_200_OK: {"description": "Context pack (Phase 5)."},
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "Not implemented yet (Phase 5).",
        },
    },
)
async def agent_context_pack(_body: Annotated[dict[str, Any], Body(default_factory=dict)]) -> None:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_YET)


__all__ = ["router"]
