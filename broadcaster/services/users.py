"""Subscriber (user) CRUD + Excel import/export.

Validation:
  - phone: required, exactly 10 digits, UNIQUE
  - email: optional, basic format
  - department, location: optional free text
  - is_active: defaults to true

Excel import (v1): upsert by phone. On phone conflict, update other fields.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException, UploadFile, status
from openpyxl import Workbook, load_workbook

from broadcaster.db import get_db

PHONE_RE = re.compile(r"^\d{10}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_phone(phone: str) -> str:
    if not isinstance(phone, str) or not PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="invalid_phone")
    return phone


def _validate_email(email: Optional[str]) -> Optional[str]:
    if email is None or email == "":
        return None
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid_email")
    return email


# ── List / get ────────────────────────────────────────────────

def list_users(
    active_only: bool = False,
    q: Optional[str] = None,
    dept: Optional[str] = None,
    location: Optional[str] = None,
) -> list[dict]:
    where: list[str] = []
    params: list = []
    if active_only:
        where.append("is_active = 1")
    if q:
        where.append("(name LIKE ? OR phone LIKE ? OR email LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if dept:
        where.append("department = ?")
        params.append(dept)
    if location:
        where.append("location = ?")
        params.append(location)

    sql = "SELECT id, name, phone, email, department, location, is_active, created_at FROM users"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_user(uid: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, phone, email, department, location, is_active, created_at "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


# ── Create / update / delete ──────────────────────────────────

def create_user(
    name: str,
    phone: str,
    email: Optional[str] = None,
    department: Optional[str] = None,
    location: Optional[str] = None,
    is_active: bool = True,
) -> dict:
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name_required")
    phone = _validate_phone(phone)
    email = _validate_email(email)

    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (name, phone, email, department, location, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name.strip(), phone, email,
                 (department or None), (location or None),
                 1 if is_active else 0, _now()),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="phone_taken")
            raise
    return get_user(cur.lastrowid)  # type: ignore[return-value]


def update_user(uid: int, **fields) -> Optional[dict]:
    allowed = {"name", "phone", "email", "department", "location", "is_active"}
    sets: list[str] = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "phone":
            v = _validate_phone(v)
        if k == "email":
            v = _validate_email(v)
        if k == "is_active":
            v = 1 if v else 0
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return get_user(uid)
    params.append(uid)
    with get_db() as conn:
        try:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="phone_taken")
            raise
    return get_user(uid)


def delete_user(uid: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    return cur.rowcount > 0


# ── Excel ─────────────────────────────────────────────────────

EXCEL_HEADERS = ["name", "phone", "email", "department", "location", "is_active"]


def _row_to_dict(row: Iterable) -> dict:
    cells = list(row) + [None] * (len(EXCEL_HEADERS) - len(list(row)))
    return {
        "name": (str(cells[0]).strip() if cells[0] is not None else ""),
        "phone": (str(cells[1]).strip() if cells[1] is not None else ""),
        "email": (str(cells[2]).strip() if cells[2] is not None else ""),
        "department": (str(cells[3]).strip() if cells[3] is not None else ""),
        "location": (str(cells[4]).strip() if cells[4] is not None else ""),
        "is_active": (str(cells[5]).strip().lower() in ("1", "true", "yes", "y", "active")
                      if cells[5] is not None else True),
    }


def import_from_xlsx(file: UploadFile, upsert: bool = True) -> dict:
    """Read first sheet. Validate each row. Upsert by phone (default).

    Returns {inserted, updated, skipped, errors: [{row, reason}]}
    """
    try:
        content = file.file.read()
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid_xlsx: {e}")

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}

    # Detect header row: if first row matches known header names, skip it.
    first = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    has_header = any(h in EXCEL_HEADERS for h in first)
    data_rows = rows[1:] if has_header else rows

    inserted = updated = skipped = 0
    errors: list[dict] = []

    with get_db() as conn:
        for idx, row in enumerate(data_rows, start=2 if has_header else 1):
            d = _row_to_dict(row)
            if not d["name"] or not d["phone"]:
                errors.append({"row": idx, "reason": "name_or_phone_missing"})
                skipped += 1
                continue
            if not PHONE_RE.match(d["phone"]):
                errors.append({"row": idx, "reason": "invalid_phone"})
                skipped += 1
                continue
            if d["email"] and not EMAIL_RE.match(d["email"]):
                errors.append({"row": idx, "reason": "invalid_email"})
                skipped += 1
                continue

            existing = conn.execute(
                "SELECT id FROM users WHERE phone = ?", (d["phone"],)
            ).fetchone()

            try:
                if existing and upsert:
                    conn.execute(
                        "UPDATE users SET name=?, email=?, department=?, location=?, is_active=? "
                        "WHERE id = ?",
                        (d["name"], d["email"] or None, d["department"] or None,
                         d["location"] or None, 1 if d["is_active"] else 0,
                         existing["id"]),
                    )
                    updated += 1
                elif existing and not upsert:
                    errors.append({"row": idx, "reason": "phone_taken"})
                    skipped += 1
                else:
                    conn.execute(
                        "INSERT INTO users (name, phone, email, department, location, is_active, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (d["name"], d["phone"], d["email"] or None,
                         d["department"] or None, d["location"] or None,
                         1 if d["is_active"] else 0, _now()),
                    )
                    inserted += 1
            except Exception as e:
                errors.append({"row": idx, "reason": f"db_error: {e}"})
                skipped += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}


def export_to_xlsx() -> bytes:
    """Return an in-memory .xlsx of all users."""
    wb = Workbook()
    ws = wb.active
    ws.title = "users"
    ws.append(EXCEL_HEADERS)
    for u in list_users():
        ws.append([
            u["name"], u["phone"], u["email"] or "",
            u["department"] or "", u["location"] or "",
            "active" if u["is_active"] else "inactive",
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
