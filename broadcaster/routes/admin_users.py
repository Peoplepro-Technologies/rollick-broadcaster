"""Admin users router — CRUD + Excel import/export + preview."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from broadcaster.routes.admin_auth import require_admin
from broadcaster.services import users as users_svc

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
def list_users(
    active_only: bool = False,
    q: str | None = None,
    dept: str | None = None,
    location: str | None = None,
):
    return users_svc.list_users(active_only=active_only, q=q, dept=dept, location=location)


@router.post("")
def create_user(payload: dict):
    name = payload.get("name")
    phone = payload.get("phone")
    if not name or not phone:
        raise HTTPException(status_code=400, detail="name_and_phone_required")
    return users_svc.create_user(
        name=name,
        phone=phone,
        email=payload.get("email"),
        department=payload.get("department"),
        location=payload.get("location"),
        is_active=bool(payload.get("is_active", True)),
    )


@router.get("/download")
def download():
    """Excel export of the full user list."""
    blob = users_svc.export_to_xlsx()
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="users.xlsx"'},
    )


@router.post("/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    upsert: bool = Query(default=True),
):
    return users_svc.import_from_xlsx(file, upsert=upsert)


@router.get("/preview")
def preview(
    q: str | None = None,
    dept: str | None = None,
    location: str | None = None,
):
    """Return the same list as `list_users` but under a /preview alias for the
    import-validate flow. Kept distinct so the front-end can show 'would-import'.
    """
    return users_svc.list_users(q=q, dept=dept, location=location)


@router.get("/{uid}")
def get_user(uid: int):
    u = users_svc.get_user(uid)
    if not u:
        raise HTTPException(status_code=404, detail="not_found")
    return u


@router.patch("/{uid}")
def update_user(uid: int, payload: dict):
    u = users_svc.update_user(uid, **payload)
    if not u:
        raise HTTPException(status_code=404, detail="not_found")
    return u


@router.delete("/{uid}")
def delete_user(uid: int):
    if not users_svc.delete_user(uid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}
