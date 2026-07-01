"""Admin user management.

Bootstrap: on first startup, if no admin exists, create one from
env credentials (ADMIN_USERNAME, ADMIN_PASSWORD). This is the only
way an admin record is created in v1; future versions may add
invite flows.

Verify: check plain password against the bcrypt hash in `admins.password_hash`.

RBAC (2026-07-01 refactor): every admin row carries a `role` and the
operations here guard against removing the last super_admin via
`LastSuperAdminError`.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from broadcaster.db import get_db
from broadcaster.security import hash_password, verify_password
from broadcaster.settings import get_settings


class LastSuperAdminError(Exception):
    """Raised when an operation would remove the last super_admin."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bootstrap_admin() -> None:
    """If no admin exists, create one from env. Idempotent.

    The bootstrap user always becomes a super_admin unless
    ADMIN_BOOTSTRAP_ROLE explicitly overrides the role. We log a
    warning when the override is set to a non-super role to flag
    lockout risk on fresh installs.
    """
    settings = get_settings()
    role = os.environ.get("ADMIN_BOOTSTRAP_ROLE", "super_admin")
    if role != "super_admin":
        # Logged but not fatal; this is a config-time concern.
        print(
            f"[bootstrap] warning: ADMIN_BOOTSTRAP_ROLE={role!r} "
            f"creates a non-super_admin bootstrap user. Risk of lockout "
            f"if no other super_admin exists."
        )
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()
        if row["n"] > 0:
            return
        conn.execute(
            "INSERT INTO admins "
            "(username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                settings.admin_username,
                hash_password(settings.admin_password),
                role,
                _now(),
            ),
        )


def find_by_username(username: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, password_hash, role, created_at "
            "FROM admins WHERE username = ?",
            (username,),
        ).fetchone()


def find_by_id(admin_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, password_hash, role, created_at "
            "FROM admins WHERE id = ?",
            (admin_id,),
        ).fetchone()


def authenticate(username: str, password: str) -> Optional[sqlite3.Row]:
    """Return admin row on success, None on bad credentials."""
    row = find_by_username(username)
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row


def authenticate_by_id(admin_id: int, password: str) -> Optional[sqlite3.Row]:
    """Verify a password for a known admin_id (no username lookup)."""
    row = find_by_id(admin_id)
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row


def count_super_admins() -> int:
    """Return the number of admins whose role is 'super_admin'."""
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM admins WHERE role = 'super_admin'"
        ).fetchone()[0]


def set_role(admin_id: int, new_role: str) -> None:
    """Change an admin's role. Refuses to demote the last super_admin."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM admins WHERE id = ?", (admin_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"admin {admin_id} not found")
        if row["role"] == "super_admin" and new_role != "super_admin":
            if count_super_admins() <= 1:
                raise LastSuperAdminError(
                    "Cannot demote the last super_admin"
                )
        conn.execute(
            "UPDATE admins SET role = ? WHERE id = ?",
            (new_role, admin_id),
        )


def delete_admin(admin_id: int) -> None:
    """Hard-delete an admin. Refuses to remove the last super_admin."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM admins WHERE id = ?", (admin_id,)
        ).fetchone()
        if row is None:
            return  # already gone
        if row["role"] == "super_admin" and count_super_admins() <= 1:
            raise LastSuperAdminError(
                "Cannot delete the last super_admin"
            )
        conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))


def change_password(*, admin_id: int, new_password: str) -> None:
    """Set a new password for the given admin.

    Authorization to call this is enforced upstream: self for any role,
    other admins only for super_admin.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE admins SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), admin_id),
        )


def list_admins() -> list[sqlite3.Row]:
    """Return all admins for the admin-management page."""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, role, created_at FROM admins "
            "ORDER BY created_at, id"
        ).fetchall()
