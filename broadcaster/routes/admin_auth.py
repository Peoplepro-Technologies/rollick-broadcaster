"""Admin auth router — login, logout, current-user.

Login accepts form-encoded POST (the login form). On success, sets
the session cookie and either:
  - redirects to /admin/ (Accept: text/html)
  - returns JSON {ok:true, redirect:"/admin/"} (XHR)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature

from broadcaster.services import admin as admin_svc

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
        return RedirectResponse("/admin/", status_code=303)
    return JSONResponse({"ok": True, "redirect": "/admin/"})


@router.post("/logout")
def logout(request: Request):
    request.session.pop(SESSION_KEY, None)
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return RedirectResponse("/admin/login", status_code=303)
    return JSONResponse({"ok": True})


@router.get("/me")
def me(request: Request, _admin_id: int = Depends(require_admin)):
    row = admin_svc.find_by_id(_admin_id)
    if row is None:
        # Session points to a deleted admin; clear and 401.
        request.session.pop(SESSION_KEY, None)
        raise HTTPException(status_code=401, detail="admin_not_found")
    return {"id": row["id"], "username": row["username"]}
