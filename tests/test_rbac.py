"""RBAC refactor tests.

Tests are written against fresh tmp_path SQLite databases so they
don't fight with the project-wide autouse DB isolation fixture in
`tests/conftest.py`.
"""
from __future__ import annotations

import sqlite3

import pytest

from broadcaster.db import _migrate_admins_role


# ── Task 1: migration ──────────────────────────────────────────


def test_migrate_adds_role_column_and_backfills_legacy_admin(tmp_path):
    """A pre-existing admins table without `role` gets the column and
    existing rows are backfilled to super_admin."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE admins (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        INSERT INTO admins (username, password_hash, created_at)
        VALUES ('legacy_admin', 'fakehash', '2026-01-01T00:00:00');
        """
    )
    conn.commit()

    _migrate_admins_role(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(admins)").fetchall()}
    assert "role" in cols
    row = conn.execute(
        "SELECT role FROM admins WHERE username='legacy_admin'"
    ).fetchone()
    assert row["role"] == "super_admin"


def test_migrate_is_idempotent_when_column_exists(tmp_path):
    """Calling migration twice does not error and does not overwrite a
    pre-populated role."""
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE admins (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT DEFAULT 'super_admin',
          created_at TEXT NOT NULL
        );
        INSERT INTO admins (username, password_hash, role, created_at)
        VALUES ('existing_super', 'h', 'super_admin', '2026-01-01T00:00:00');
        """
    )
    conn.commit()

    _migrate_admins_role(conn)
    _migrate_admins_role(conn)

    row = conn.execute(
        "SELECT role FROM admins WHERE username='existing_super'"
    ).fetchone()
    assert row["role"] == "super_admin"


def test_migrate_fixes_null_roles(tmp_path):
    """Rows with NULL role get fixed even if the column already exists."""
    db = tmp_path / "null.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE admins (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT,
          created_at TEXT NOT NULL
        );
        INSERT INTO admins (username, password_hash, role, created_at)
        VALUES ('null_role', 'h', NULL, '2026-01-01T00:00:00');
        """
    )
    conn.commit()

    _migrate_admins_role(conn)

    row = conn.execute(
        "SELECT role FROM admins WHERE username='null_role'"
    ).fetchone()
    assert row["role"] == "super_admin"
