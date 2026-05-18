"""HTMX preview-panel route (#119, extracted in #129).

``GET /preview/{chunk_id}`` renders the ``_preview.html`` fragment for
a chunk plus its immediate neighbors. The route lives in its own
module so :mod:`cf_knowledge_kiln.api.web` stays under the AGENTS.md
400-line soft cap; it shares the same Jinja templates and depends only
on form-parsing helpers from :mod:`cf_knowledge_kiln.api.forms`.

Mounted by :func:`cf_knowledge_kiln.api.app.create_app` alongside the
search + feedback routes — the URL path is preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from cf_knowledge_kiln.api.dependencies import get_session
from cf_knowledge_kiln.api.forms import parse_uuid
from cf_knowledge_kiln.db.repositories import ChunksRepository, DocumentsRepository

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["web"], include_in_schema=False)

_PREVIEW_NEIGHBOR_CHARS: int = 500
"""Hard char cap on each neighbor preview body — keeps the side panel
short enough to read at a glance, per #119 acceptance criteria."""


@router.get("/preview/{chunk_id}", response_class=HTMLResponse)
async def preview_chunk(
    request: Request,
    chunk_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """HTMX target — render a chunk + its neighbors in the preview panel.

    Returns the ``_preview.html`` fragment ready to be swapped into
    ``#preview`` by HTMX. Unknown / malformed chunk ids render a small
    italic message rather than a JSON 404 so the swap stays inline.
    The neighbor bodies are trimmed to :data:`_PREVIEW_NEIGHBOR_CHARS`
    characters; the target chunk renders in full.
    """
    cid = parse_uuid(chunk_id)
    if cid is None:
        return _templates.TemplateResponse(
            request, "_preview.html", {"missing": True}, status_code=404
        )
    prev, target, nxt = await ChunksRepository(session).neighbors(cid, n=1)
    if target is None:
        return _templates.TemplateResponse(
            request, "_preview.html", {"missing": True}, status_code=404
        )
    doc = await DocumentsRepository(session).get(target.document_id)
    return _templates.TemplateResponse(
        request,
        "_preview.html",
        {
            "missing": False,
            "doc": doc,
            "target": {
                "chunk_id": target.id,
                "chunk_index": target.chunk_index,
                "heading_path": list(target.heading_path or []),
                "content": target.content,
            },
            "prev": [
                {
                    "chunk_id": c.id,
                    "chunk_index": c.chunk_index,
                    "content": c.content[:_PREVIEW_NEIGHBOR_CHARS],
                    "truncated": len(c.content) > _PREVIEW_NEIGHBOR_CHARS,
                }
                for c in prev
            ],
            "next": [
                {
                    "chunk_id": c.id,
                    "chunk_index": c.chunk_index,
                    "content": c.content[:_PREVIEW_NEIGHBOR_CHARS],
                    "truncated": len(c.content) > _PREVIEW_NEIGHBOR_CHARS,
                }
                for c in nxt
            ],
        },
    )


__all__ = ["router"]
