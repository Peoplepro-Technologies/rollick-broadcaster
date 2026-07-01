"""Admin groups router — CRUD + auto-group rebuild + membership.

RBAC: super_admin and hr_admin only. Management is read-only across
the rest of the app but does not see groups (sensitive: contains
membership, criteria). hr_admin owns users and groups.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from broadcaster.rbac import load_current_admin, require_role
from broadcaster.services import groups as groups_svc

router = APIRouter(
    prefix="/api/groups",
    tags=["groups"],
    dependencies=[
        Depends(load_current_admin),
        Depends(require_role("super_admin", "hr_admin")),
    ],
)


@router.get("")
def list_groups():
    return groups_svc.list_groups()


@router.post("")
def create_group(payload: dict):
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name_required")
    return groups_svc.create_manual_group(
        name=name,
        type_=payload.get("type", "manual"),
        criteria=payload.get("criteria"),
    )


@router.post("/rebuild-auto")
def rebuild_auto():
    return groups_svc.rebuild_auto_groups()


@router.get("/{gid}")
def get_group(gid: int):
    g = groups_svc.get_group(gid)
    if not g:
        raise HTTPException(status_code=404, detail="not_found")
    return g


@router.patch("/{gid}")
def update_group(gid: int, payload: dict):
    g = groups_svc.update_group(
        gid,
        name=payload.get("name"),
        criteria=payload.get("criteria"),
        type_=payload.get("type"),
    )
    if not g:
        raise HTTPException(status_code=404, detail="not_found")
    return g


@router.delete("/{gid}")
def delete_group(gid: int):
    if not groups_svc.delete_group(gid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@router.get("/{gid}/members")
def get_members(gid: int):
    if not groups_svc.get_group(gid):
        raise HTTPException(status_code=404, detail="not_found")
    return groups_svc.get_members(gid)


@router.post("/{gid}/members")
def add_members(gid: int, payload: dict):
    uids = payload.get("user_ids") or []
    if not isinstance(uids, list) or not all(isinstance(u, int) for u in uids):
        raise HTTPException(status_code=400, detail="user_ids_must_be_list_of_ints")
    n = groups_svc.add_members(gid, uids)
    return {"added": n}


@router.delete("/{gid}/members/{uid}")
def remove_member(gid: int, uid: int):
    if not groups_svc.remove_member(gid, uid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}
