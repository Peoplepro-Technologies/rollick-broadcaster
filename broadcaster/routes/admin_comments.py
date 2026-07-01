"""Admin comment moderation router.

RBAC:
  - Read (list): super_admin, content_admin, management.
  - Mutating (hide/unhide/delete/flag): super_admin, content_admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from broadcaster.rbac import load_current_admin, require_role
from broadcaster.services import comments as comments_svc

READ_ROLES = ("super_admin", "content_admin", "management")
WRITE_ROLES = ("super_admin", "content_admin")

router = APIRouter(
    prefix="/api/comments",
    tags=["comments"],
    dependencies=[Depends(load_current_admin)],
)


@router.get("", dependencies=[Depends(require_role(*READ_ROLES))])
def list_comments(
    broadcast_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
):
    return comments_svc.list_all(broadcast_id=broadcast_id, status=status, q=q)


@router.patch("/{cid}", dependencies=[Depends(require_role(*WRITE_ROLES))])
def update_comment(cid: int, payload: dict):
    new_status = payload.get("status")
    if new_status not in ("visible", "hidden"):
        raise HTTPException(status_code=400, detail="invalid_status")
    if not comments_svc.get_comment(cid):
        raise HTTPException(status_code=404, detail="not_found")
    if new_status == "visible":
        comments_svc.unhide(cid)
    else:
        comments_svc.hide(cid)
    return comments_svc.get_comment(cid)


@router.delete("/{cid}", dependencies=[Depends(require_role(*WRITE_ROLES))])
def delete_comment(cid: int):
    if not comments_svc.delete(cid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@router.post("/{cid}/flag", dependencies=[Depends(require_role(*WRITE_ROLES))])
def flag_comment(cid: int):
    if not comments_svc.flag(cid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}