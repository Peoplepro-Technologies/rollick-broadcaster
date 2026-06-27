"""Admin user management.

Bootstrap: on first startup, if no admin exists, create one from
env credentials (ADMIN_USERNAME, ADMIN_PASSWORD). This is the only
way an admin record is created in v1; future versions may add
invite flows.

Verify: check plain password against the bcrypt hash in `admins.password_hash`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from broadcaster.db import get_db
from broadcaster.security import hash_password, verify_password
from broadcaster.settings import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bootstrap_admin() -> None:
    """If no admin exists, create one from env. Idempotent."""
    settings = get_settings()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()
        if row["n"] > 0:
            return
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?, ?, ?)",
            (settings.admin_username, hash_password(settings.admin_password), _now()),
        )


def find_by_username(username: str) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, password_hash, created_at FROM admins WHERE username = ?",
            (username,),
        ).fetchone()


def find_by_id(admin_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, password_hash, created_at FROM admins WHERE id = ?",
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
