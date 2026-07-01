"""Admin-management router — super_admin only.

Routes for managing other admin accounts: create, change role,
change password. Self-password change is handled at /api/auth/change-password
(it lives in admin_auth.py because it doesn't require super_admin).

The lockout guard for the last super_admin lives in
`broadcaster.services.admin.set_role` / `delete_admin`. We propagate
LastSuperAdminError as HTTP 409.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status

from broadcaster.rbac import (
    AdminUser,
    load_current_admin,
    require_role,
)
from broadcaster.services import admin as admin_svc

router = APIRouter(
    prefix="/api/admins",
    tags=["admins"],
    dependencies=[
        Depends(load_current_admin),
        Depends(require_role("super_admin")),
    ],
)


def _is_self(current: AdminUser, target_id: int) -> bool:
    return current.id == target_id


@router.get("")
def list_admins():
    """Return all admin accounts. super_admin only."""
    rows = admin_svc.list_admins()
    return [dict(r) for r in rows]


@router.post("")
def create_admin(payload: dict = Body(...)):
    """Create a new admin. super_admin only.

    Required keys: `username`, `password`, `role` ∈
    {super_admin, hr_admin, content_admin, management}.
    """
    username = payload.get("username")
    password = payload.get("password")
    role = payload.get("role")
    if not username or not password or not role:
        raise HTTPException(status_code=400, detail="username_password_role_required")
    if role not in ("super_admin", "hr_admin", "content_admin", "management"):
        raise HTTPException(status_code=400, detail="invalid_role")
    # Reject duplicates early.
    if admin_svc.find_by_username(username) is not None:
        raise HTTPException(status_code=409, detail="username_taken")
    try:
        admin_id = admin_svc.create_admin(
            username=username, password=password, role=role
        )
    except admin_svc.LastSuperAdminError as exc:
        # Defensive — create_admin doesn't enforce lockout, but future
        # hardening could (e.g. demotion-on-create).
        raise HTTPException(status_code=409, detail=str(exc))
    return {"id": admin_id, "username": username, "role": role}


@router.post("/{admin_id}/role")
def set_role(admin_id: int, payload: dict = Body(...)):
    """Change another admin's role. super_admin only.

    LastSuperAdminError is translated to HTTP 409.
    """
    new_role = payload.get("role")
    if new_role not in ("super_admin", "hr_admin", "content_admin", "management"):
        raise HTTPException(status_code=400, detail="invalid_role")
    try:
        admin_svc.set_role(admin_id, new_role)
    except admin_svc.LastSuperAdminError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    row = admin_svc.find_by_id(admin_id)
    return dict(row)


@router.post("/{admin_id}/password")
def change_password(admin_id: int, payload: dict = Body(...)):
    """Change another admin's password. super_admin only.

    Self password changes go through /api/auth/change-password
    (which accepts any role).
    """
    new_password = payload.get("password")
    if not new_password:
        raise HTTPException(status_code=400, detail="password_required")
    if admin_svc.find_by_id(admin_id) is None:
        raise HTTPException(status_code=404, detail="admin_not_found")
    admin_svc.change_password(admin_id=admin_id, new_password=new_password)
    return {"ok": True}


@router.delete("/{admin_id}")
def delete_admin(admin_id: int, current: AdminUser = Depends(load_current_admin)):
    """Delete an admin. super_admin only. Refuses to remove the
    last super_admin (LastSuperAdminError → HTTP 409)."""
    if _is_self(current, admin_id):
        raise HTTPException(status_code=400, detail="cannot_delete_self")
    try:
        admin_svc.delete_admin(admin_id)
    except admin_svc.LastSuperAdminError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"ok": True}
