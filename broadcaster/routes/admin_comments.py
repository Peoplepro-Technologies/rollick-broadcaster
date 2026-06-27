"""Admin comment moderation router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from broadcaster.routes.admin_auth import require_admin
from broadcaster.services import comments as comments_svc

router = APIRouter(
    prefix="/api/comments",
    tags=["comments"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
def list_comments(
    broadcast_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
):
    return comments_svc.list_all(broadcast_id=broadcast_id, status=status, q=q)


@router.patch("/{cid}")
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


@router.delete("/{cid}")
def delete_comment(cid: int):
    if not comments_svc.delete(cid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@router.post("/{cid}/flag")
def flag_comment(cid: int):
    if not comments_svc.flag(cid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}