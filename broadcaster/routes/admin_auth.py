"""Admin auth router — login, logout, current-user, forgot/change-password.

Login accepts form-encoded POST (the login form). On success, sets
the session cookie and either:
  - redirects to /admin/ (Accept: text/html)
  - returns JSON {ok:true, redirect:"/admin/"} (XHR)

Forgotten-password flow: POST /api/auth/forgot-password {username} →
generates a temp password, emails it to the configured recovery
mailbox, and sets must_change_password so the admin is forced to set
a permanent password on first sign-in. POST /api/auth/change-password
clears that flag.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature

from broadcaster.security import hash_password, verify_password
from broadcaster.services import admin as admin_svc
from broadcaster.services import password_reset as password_reset_svc
from broadcaster.rbac import AdminUser, load_current_admin


# Re-export for routes that imported require_admin from this module
# before the RBAC refactor. Will be removed once all admin route
# files use broadcaster.rbac directly.
require_admin = load_current_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


SESSION_KEY = "admin_id"


def current_admin_id(request: Request) -> int | None:
    """Read the admin id from the signed session cookie, if present."""
    return request.session.get(SESSION_KEY)


def require_admin(request: Request) -> int:
    """Dependency: raise 401 if no valid session."""
    admin_id = current_admin_id(request)
    if admin_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    return admin_id


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    row = admin_svc.authenticate(username, password)
    if row is None:
        # Match the dual-response style below
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse("/admin/login?error=1", status_code=303)
        raise HTTPException(status_code=401, detail="invalid_credentials")

    request.session[SESSION_KEY] = row["id"]

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        # If must_change_password is set, route the browser to the
        # change page (the load_current_admin dependency will enforce
        # the same rule on every subsequent request anyway).
        if row["must_change_password"]:
            return RedirectResponse("/admin/change-password", status_code=303)
        return RedirectResponse("/admin/", status_code=303)
    return JSONResponse({
        "ok": True,
        "redirect": "/admin/change-password" if row["must_change_password"] else "/admin/",
        "must_change_password": bool(row["must_change_password"]),
    })


@router.post("/logout")
def logout(request: Request):
    request.session.pop(SESSION_KEY, None)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse("/admin/login", status_code=303)
    return JSONResponse({"ok": True})


@router.get("/me")
def me(request: Request, _admin: AdminUser = Depends(load_current_admin)):
    return {
        "id": _admin.id,
        "username": _admin.username,
        "role": _admin.role,
    }


# ── Forgot password ────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(payload: dict = Body(...)):
    """Mint a temporary password for `username` and email it to the
    configured recovery mailbox. Always returns 200 with `{ok, detail}`
    unless `username` is missing — explicit failure codes per the
    strict-errors UX choice.
    """
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username_required")
    ok, detail = password_reset_svc.request_reset(username)
    if not ok:
        # Map service detail codes to 400 — they describe client-side
        # misconfigurations (missing recovery mailbox, etc.).
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail}


# ── Forced-change on first login ───────────────────────────────

@router.post("/change-password")
def change_password(payload: dict = Body(...), request: Request = None):
    """Set a new password for the currently signed-in admin.

    Validates `old_password` against the stored hash, requires `new`
    and `confirm` to match, requires `new` to be ≥ 8 characters, and
    clears the `must_change_password` flag on success. Returns 401 if
    no session.
    """
    admin_id = current_admin_id(request)
    if admin_id is None:
        raise HTTPException(status_code=401, detail="not_authenticated")

    old = payload.get("old_password") or ""
    new = payload.get("new_password") or ""
    confirm = payload.get("confirm") or ""

    if not old or not new or not confirm:
        raise HTTPException(status_code=400, detail="all_fields_required")
    if new != confirm:
        raise HTTPException(status_code=400, detail="confirm_mismatch")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="password_too_short")

    row = admin_svc.find_by_id(admin_id)
    if row is None or not verify_password(old, row["password_hash"]):
        raise HTTPException(status_code=400, detail="wrong_old_password")

    admin_svc.change_password(admin_id=admin_id, new_password=new)
    admin_svc.set_must_change_password(admin_id, False)
    return {"ok": True}
