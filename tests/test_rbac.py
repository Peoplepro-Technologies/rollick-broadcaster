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


# ── Task 2: service-layer role ops + lockout ───────────────────


@pytest.fixture
def isolated_admin_db(tmp_path, monkeypatch):
    """Yield a fresh DB URL pointing at a tmp file, with the admins table
    created and one super_admin row seeded."""
    db_path = tmp_path / "svc.db"
    monkeypatch.setenv("DATABASE_URL", str(db_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("IP_HASH_PEPPER", "test-pepper")
    from broadcaster.settings import bust_settings_cache
    bust_settings_cache()
    from broadcaster.db import _connect, init_db
    init_db()
    yield db_path


def _seed(isolated_admin_db, role: str = "super_admin", username: str | None = None) -> int:
    """Insert an admin in the role, return its id."""
    from broadcaster.db import get_db
    from broadcaster.security import hash_password
    username = username or f"u_{role}_{abs(hash(role))}"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password("x"), role, "2026-01-01T00:00:00"),
        )
        return cur.lastrowid


def test_find_by_id_includes_role(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    _seed(isolated_admin_db, role="content_admin", username="ca")
    row = admin_svc.find_by_id(_seed(isolated_admin_db, role="super_admin", username="sa"))
    # The freshly-inserted super_admin should have role returned.
    sa_id = _seed(isolated_admin_db, role="super_admin", username="sa2")
    r = admin_svc.find_by_id(sa_id)
    assert r["role"] == "super_admin"


def test_set_role_changes_role(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    aid = _seed(isolated_admin_db, role="hr_admin", username="hr1")
    admin_svc.set_role(aid, "content_admin")
    assert admin_svc.find_by_id(aid)["role"] == "content_admin"


def test_set_role_blocked_for_last_super_admin(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    aid = _seed(isolated_admin_db, role="super_admin", username="only_sa")
    with pytest.raises(admin_svc.LastSuperAdminError):
        admin_svc.set_role(aid, "hr_admin")


def test_set_role_allowed_when_other_super_admin_exists(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    a = _seed(isolated_admin_db, role="super_admin", username="sa_a")
    b = _seed(isolated_admin_db, role="super_admin", username="sa_b")
    admin_svc.set_role(a, "hr_admin")
    assert admin_svc.find_by_id(a)["role"] == "hr_admin"


def test_delete_admin_blocked_for_last_super_admin(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    aid = _seed(isolated_admin_db, role="super_admin", username="only_sa")
    with pytest.raises(admin_svc.LastSuperAdminError):
        admin_svc.delete_admin(aid)


def test_change_password_self(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    aid = _seed(isolated_admin_db, role="hr_admin", username="hrpw")
    admin_svc.change_password(admin_id=aid, new_password="brand-new-123")
    assert admin_svc.authenticate_by_id(aid, "brand-new-123") is not None
    # Old password no longer works.
    assert admin_svc.authenticate_by_id(aid, "x") is None


def test_count_super_admins(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    assert admin_svc.count_super_admins() == 0
    _seed(isolated_admin_db, role="super_admin", username="one")
    assert admin_svc.count_super_admins() == 1
    _seed(isolated_admin_db, role="hr_admin", username="hr")
    assert admin_svc.count_super_admins() == 1
    _seed(isolated_admin_db, role="super_admin", username="two")
    assert admin_svc.count_super_admins() == 2


def test_list_admins(isolated_admin_db):
    from broadcaster.services import admin as admin_svc
    _seed(isolated_admin_db, role="super_admin", username="alpha")
    _seed(isolated_admin_db, role="hr_admin", username="beta")
    rows = admin_svc.list_admins()
    usernames = {r["username"] for r in rows}
    assert {"alpha", "beta"}.issubset(usernames)


def test_bootstrap_writes_super_admin_role(isolated_admin_db):
    """bootstrap_admin() must populate the role column."""
    from broadcaster.db import get_db
    from broadcaster.services import admin as admin_svc
    # isolated fixture seeds nothing; bootstrap creates one.
    admin_svc.bootstrap_admin()
    with get_db() as conn:
        row = conn.execute("SELECT role FROM admins").fetchone()
    assert row["role"] == "super_admin"


# ── Task 4: broadcaster/rbac.py module ────────────────────────


def test_role_lanes_keys():
    from broadcaster.rbac import ROLE_LANES
    assert set(ROLE_LANES.keys()) == {
        "super_admin", "hr_admin", "content_admin", "management",
    }


def test_role_rank_ordering():
    from broadcaster.rbac import ROLE_RANK
    assert ROLE_RANK["super_admin"] > ROLE_RANK["hr_admin"]
    assert ROLE_RANK["hr_admin"] > ROLE_RANK["content_admin"]
    assert ROLE_RANK["content_admin"] > ROLE_RANK["management"]


def test_admin_user_dataclass():
    from broadcaster.rbac import AdminUser
    u = AdminUser(id=1, username="x", role="hr_admin")
    assert u.id == 1
    assert u.username == "x"
    assert u.role == "hr_admin"
    # Frozen.
    with pytest.raises(Exception):
        u.role = "super_admin"  # type: ignore[misc]


def test_load_current_admin_401_when_no_session(isolated_admin_db):
    """Without a session cookie, the dependency raises 401."""
    from starlette.requests import Request

    from broadcaster.rbac import load_current_admin

    class _FakeReq:
        session: dict = {}

        def __init__(self):
            pass

    req = _FakeReq()
    req.session = {}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        load_current_admin(req)
    assert exc.value.status_code == 401
