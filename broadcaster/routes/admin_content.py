"""Admin content router — text snippets + media upload + delete.

RBAC:
  - Read endpoints (list/get/serve): super_admin, content_admin, management.
  - Mutating endpoints (text/media/create/delete): super_admin,
    content_admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from broadcaster.rbac import load_current_admin, require_role
from broadcaster.services import content as content_svc

READ_ROLES = ("super_admin", "content_admin", "management")
WRITE_ROLES = ("super_admin", "content_admin")

router = APIRouter(
    prefix="/api/content",
    tags=["content"],
    dependencies=[Depends(load_current_admin)],
)


@router.get("", dependencies=[Depends(require_role(*READ_ROLES))])
def list_content():
    return content_svc.list_content()


@router.post("/text", dependencies=[Depends(require_role(*WRITE_ROLES))])
def create_text(payload: dict):
    return content_svc.create_text(
        caption=payload.get("caption"),
        body=payload.get("body", ""),
    )


@router.post("/media", dependencies=[Depends(require_role(*WRITE_ROLES))])
async def upload_media(
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
):
    return content_svc.create_media(file, caption=caption)


@router.get("/{cid}", dependencies=[Depends(require_role(*READ_ROLES))])
def get_content(cid: int):
    c = content_svc.get_content(cid)
    if not c:
        raise HTTPException(status_code=404, detail="not_found")
    return c


@router.delete("/{cid}", dependencies=[Depends(require_role(*WRITE_ROLES))])
def delete_content(cid: int):
    if not content_svc.delete_content(cid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


# ── Admin-only file serve ────────────────────────────────────

@router.get("/file/{cid}", dependencies=[Depends(require_role(*READ_ROLES))])
def serve_media(cid: int):
    """Serve a media file to the admin. Subscribers use /v/{token}/media (Phase 3)."""
    c = content_svc.get_content(cid)
    if not c or c["content_type"] != "media":
        raise HTTPException(status_code=404, detail="not_found")
    path = content_svc._resolve_content_path(c["content_data"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file_missing")
    return FileResponse(
        path,
        media_type=c.get("mime_type") or "application/octet-stream",
        filename=c.get("file_name") or path.name,
    )
