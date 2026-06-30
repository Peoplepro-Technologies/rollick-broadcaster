"""Admin users router — CRUD + Excel import/export + preview."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from broadcaster.routes.admin_auth import require_admin
from broadcaster.services import users as users_svc

# Cap upload size so a 1M-row spreadsheet doesn't pin the browser/server.
# 10 MiB is comfortably larger than any realistic subscriber list export
# (a row is ~100 bytes; 10 MiB ≈ 100k rows).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

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


@router.get("/template")
def template():
    """Excel template — blank xlsx with just the header row. Use this to
    bulk-add users without first exporting the live list."""
    blob = users_svc.export_template_xlsx()
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="users_template.xlsx"'},
    )


@router.post("/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
):
    # Reject the upload before reading it into memory; gives the user a
    # clear 413 instead of letting the browser hang on a giant POST.
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file_too_large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )
    return users_svc.import_from_xlsx(file)


@router.post("/upload-excel/errors.csv")
async def upload_excel_errors_csv(payload: dict = Body(...)):
    """Return the same `errors[]` array as RFC-4180 CSV. 400 when empty."""
    errors = payload.get("errors") or []
    if not isinstance(errors, list) or not errors:
        raise HTTPException(status_code=400, detail="no_errors")
    blob = users_svc.import_to_csv_errors(errors)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=blob,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="users_import_errors_{stamp}.csv"'},
    )


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
