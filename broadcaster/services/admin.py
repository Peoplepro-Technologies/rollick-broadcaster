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
            "SELECT id, username, password_hash, role, recovery_email, "
            "created_at, must_change_password "
            "FROM admins WHERE username = ?",
            (username,),
        ).fetchone()


def find_by_id(admin_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, password_hash, role, recovery_email, "
            "created_at, must_change_password "
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


def set_must_change_password(admin_id: int, value: bool) -> None:
    """Mark (or unmark) an admin as needing a password change on next
    sign-in. Used by the forgot-password flow after generating a
    temporary password; cleared by /api/auth/change-password once the
    admin sets a permanent one.
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE admins SET must_change_password = ? WHERE id = ?",
            (1 if value else 0, admin_id),
        )


def set_recovery_email(admin_id: int, recovery_email: str) -> None:
    """Set the per-admin recovery email.

    Empty / whitespace-only input raises `ValueError("recovery_email_required")`
    so the caller can map it to a 400 with `detail="recovery_email_required"`.
    Use this entry point when the row already exists; callers SHOULD pass
    a value validated through `validate_email(..., required=True)`.
    """
    email = (recovery_email or "").strip()
    if not email:
        raise ValueError("recovery_email_required")
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM admins WHERE id = ?", (admin_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"admin {admin_id} not found")
        conn.execute(
            "UPDATE admins SET recovery_email = ? WHERE id = ?",
            (email, admin_id),
        )


def resolve_recovery_email(admin_row: sqlite3.Row) -> str | None:
    """Recipient for the forgot-password flow.

    Per-admin row wins; falls back to the global `password_recovery_email`
    setting when the row has no email. Returns `None` when neither is
    set, so the caller can raise `recovery_mailbox_not_configured`.
    """
    per_admin = (admin_row["recovery_email"] or "").strip()
    if per_admin:
        return per_admin
    # Lazy import: broadcaster.services.settings imports broadcaster.db
    # which imports broadcaster.services.admin at module load.
    from broadcaster.services import settings as settings_svc
    global_email = (settings_svc.get("password_recovery_email") or "").strip()
    return global_email or None


def list_admins() -> list[sqlite3.Row]:
    """Return all admins for the admin-management page."""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, role, recovery_email, created_at FROM admins "
            "ORDER BY created_at, id"
        ).fetchall()


def create_admin(
    *, username: str, password: str, role: str, recovery_email: str = ""
) -> int:
    """Create a new admin row. Returns the new id.

    `recovery_email` defaults to `''` for backward compatibility with
    legacy callers that pre-date the per-admin recovery flow. New admin
    creation routes MUST validate it via `validate_email(recovery_email,
    required=True)` BEFORE calling this function and reject empty input.
    """
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO admins (username, password_hash, role, "
            "recovery_email, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), role,
             (recovery_email or "").strip(), _now()),
        )
        return cur.lastrowid
