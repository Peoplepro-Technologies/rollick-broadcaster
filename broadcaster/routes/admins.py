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
from broadcaster.services import password_reset as password_reset_svc
from broadcaster.services.users import validate_email

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
    {super_admin, hr_admin, content_admin, management},
    `recovery_email` (RFC-5322-ish — validated via
    services.users.validate_email with required=True).

    Per-admin recovery_email was added 2026-07-14; every admin row
    created from here onward carries the inbox the forgot-password flow
    routes to (with the global settings.password_recovery_email as
    fallback when the row is later cleared).
    """
    username = payload.get("username")
    password = payload.get("password")
    role = payload.get("role")
    recovery_email = payload.get("recovery_email") or ""
    if not username or not password or not role:
        raise HTTPException(status_code=400, detail="username_password_role_required")
    if role not in ("super_admin", "hr_admin", "content_admin", "management"):
        raise HTTPException(status_code=400, detail="invalid_role")
    # Raises 400 invalid_email on missing / malformed; matches the email
    # format contract used by services/users.py.
    validated_recovery_email = validate_email(recovery_email, required=True)
    # Reject duplicates early.
    if admin_svc.find_by_username(username) is not None:
        raise HTTPException(status_code=409, detail="username_taken")
    try:
        admin_id = admin_svc.create_admin(
            username=username, password=password, role=role,
            recovery_email=validated_recovery_email,
        )
    except admin_svc.LastSuperAdminError as exc:
        # Defensive — create_admin doesn't enforce lockout, but future
        # hardening could (e.g. demotion-on-create).
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "id": admin_id,
        "username": username,
        "role": role,
        "recovery_email": validated_recovery_email,
    }


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


@router.post("/{admin_id}/recovery-email")
def set_recovery_email(admin_id: int, payload: dict = Body(...)):
    """Set or update another admin's recovery email. super_admin only.

    Required keys: `recovery_email` (validated via services.users
    .validate_email with required=True). Returns 404 if the admin row
    is gone; 400 with `detail=invalid_email` on missing / malformed
    input; 200 with the updated row on success.
    """
    new_email = payload.get("recovery_email") or ""
    validated = validate_email(new_email, required=True)
    try:
        admin_svc.set_recovery_email(admin_id, validated)
    except ValueError as exc:
        # set_recovery_email raises ValueError when the row is missing
        # (or when input is empty). For empty input the validate_email
        # call above already converted it to a 400, so any ValueError
        # reaching here is the missing-row case.
        raise HTTPException(status_code=404, detail=str(exc))
    row = admin_svc.find_by_id(admin_id)
    return dict(row)


@router.post("/{admin_id}/send-recovery-email")
def send_recovery_email(admin_id: int):
    """Send a fresh temporary password to the admin's recovery email
    on their behalf. super_admin only.

    Reuses `password_reset_svc.request_reset()` so the recipient-resolution
    rules (per-admin row, fall back to global setting) and SMTP rollback
    semantics are identical to the user-initiated forgot-password flow.
    Returns 200 with `{ok, detail, recipient, username}` on success so the
    UI can confirm where the temp password was routed; 404 if the admin
    row is gone; 400 with the same detail codes as the forgot-password
    route (no_such_admin, recovery_mailbox_not_configured,
    smtp_not_configured, send_failed) for the various config / send
    failure modes.
    """
    row = admin_svc.find_by_id(admin_id)
    if row is None:
        raise HTTPException(status_code=404, detail="admin_not_found")
    # Pre-compute the recipient for the success-response UI feedback.
    # request_reset will resolve it independently inside the service.
    recipient = admin_svc.resolve_recovery_email(row)
    username = row["username"]
    ok, detail = password_reset_svc.request_reset(username)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {
        "ok": True,
        "detail": detail,
        "recipient": recipient,
        "username": username,
    }


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
