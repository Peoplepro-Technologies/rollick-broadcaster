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


# ── Task 5: /api/auth/me returns role ─────────────────────────


async def test_me_returns_role(client):
    """After login, /api/auth/me echoes username, id, and role."""
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert body["role"] == "super_admin"
    assert "id" in body


# ── Task 6: route-sweep matrix ────────────────────────────────


async def _login_as(client, username: str, password: str = "test-pass"):
    """Log in as a non-default admin; seed the row if needed.

    Note: most tests use the conftest's autouse bootstrap which creates
    'admin'. For other roles we seed an extra user before logging in.
    """
    await client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )


def _seed_admin(username: str, role: str, password: str = "test-pass"):
    """Insert an admin row directly; return row id."""
    from broadcaster.db import get_db
    from broadcaster.security import hash_password
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, "2026-01-01T00:00:00"),
        )
        return cur.lastrowid


ROLE_ROUTE_MATRIX = [
    # (role, method, path, expected_status)
    # ── users (read)
    ("hr_admin",      "GET",  "/api/users",             200),
    ("content_admin", "GET",  "/api/users",             403),
    ("management",    "GET",  "/api/users",             200),
    # ── users (write) — POST without body: auth passes for allowed
    # role then validation fails with 422.
    ("hr_admin",      "POST", "/api/users",             422),
    ("management",    "POST", "/api/users",             403),
    # ── groups (read+write both share role)
    ("hr_admin",      "GET",  "/api/groups",            200),
    ("content_admin", "GET",  "/api/groups",            403),
    ("management",    "GET",  "/api/groups",            403),
    # ── content (read)
    ("content_admin", "GET",  "/api/content",           200),
    ("hr_admin",      "GET",  "/api/content",           403),
    ("management",    "GET",  "/api/content",           200),
    # ── content (write)
    ("content_admin", "POST", "/api/content/text",      422),
    ("hr_admin",      "POST", "/api/content/text",      403),
    ("management",    "POST", "/api/content/text",      403),
    # ── broadcasts (read)
    ("hr_admin",      "GET",  "/api/broadcasts",        200),
    ("management",    "GET",  "/api/broadcasts",        200),
    # ── broadcasts (write)
    ("content_admin", "POST", "/api/broadcasts",        422),  # body missing, but auth passes
    ("hr_admin",      "POST", "/api/broadcasts",        403),
    ("management",    "POST", "/api/broadcasts",        403),
    # ── comments (read)
    ("content_admin", "GET",  "/api/comments",          200),
    ("management",    "GET",  "/api/comments",          200),
    ("hr_admin",      "GET",  "/api/comments",          403),
    # ── settings (read)
    ("super_admin",   "GET",  "/api/settings",          200),
    ("management",    "GET",  "/api/settings",          200),
    ("hr_admin",      "GET",  "/api/settings",          403),
    ("content_admin", "GET",  "/api/settings",          403),
    # ── settings (write)
    ("management",    "POST", "/api/settings",          403),
    ("super_admin",   "GET",  "/api/settings/runtime",  200),
    ("management",    "GET",  "/api/settings/runtime",  200),
]


@pytest.mark.parametrize("role,method,path,expected", ROLE_ROUTE_MATRIX)
async def test_role_route_matrix(role, method, path, expected, client):
    """For each (role, route) cell, asserts the expected HTTP status.

    super_admin (the default bootstrap user 'admin') is the existing
    fixture; for other roles we seed an extra admin and log in as it.
    """
    username = f"{role}_{abs(hash((role, method, path))) % 10**8}"

    await client.post("/api/auth/logout")
    if role == "super_admin":
        await _login_as(client, "admin", password="test-admin-pass")
    else:
        _seed_admin(username, role)
        await _login_as(client, username)

    resp = await getattr(client, method.lower())(path)
    # For 200 we don't care about body shape; just status.
    assert resp.status_code == expected, (
        f"role={role} {method} {path} got {resp.status_code}, expected {expected}"
    )


# ── Task 7: page-handler role gates + nav rendering ────────────


PAGE_GATES = [
    # (role, path, expected_status)
    ("super_admin",   "/admin/",                  200),
    ("hr_admin",      "/admin/",                  200),
    ("content_admin", "/admin/",                  200),
    ("management",    "/admin/",                  200),

    ("super_admin",   "/admin/users",             200),
    ("hr_admin",      "/admin/users",             200),
    ("management",    "/admin/users",             200),
    ("content_admin", "/admin/users",             403),

    ("super_admin",   "/admin/groups",            200),
    ("hr_admin",      "/admin/groups",            200),
    ("content_admin", "/admin/groups",            403),
    ("management",    "/admin/groups",            403),

    ("super_admin",   "/admin/content",           200),
    ("content_admin", "/admin/content",           200),
    ("management",    "/admin/content",           200),
    ("hr_admin",      "/admin/content",           403),

    ("super_admin",   "/admin/broadcasts",        200),
    ("hr_admin",      "/admin/broadcasts",        200),
    ("content_admin", "/admin/broadcasts",        200),
    ("management",    "/admin/broadcasts",        200),

    ("super_admin",   "/admin/broadcasts/new",    200),
    ("content_admin", "/admin/broadcasts/new",    200),
    ("hr_admin",      "/admin/broadcasts/new",    403),
    ("management",    "/admin/broadcasts/new",    403),

    ("super_admin",   "/admin/comments",          200),
    ("content_admin", "/admin/comments",          200),
    ("management",    "/admin/comments",          200),
    ("hr_admin",      "/admin/comments",          403),

    ("super_admin",   "/admin/settings",          200),
    ("management",    "/admin/settings",          200),
    ("hr_admin",      "/admin/settings",          403),
    ("content_admin", "/admin/settings",          403),
]


@pytest.mark.parametrize("role,path,expected", PAGE_GATES)
async def test_page_handler_role_gate(role, path, expected, client):
    """Page-rendering endpoints in app.py must enforce the spec's
    capability mapping (separate from the JSON API route guards)."""
    username = f"page_{role}_{abs(hash((role, path))) % 10**8}"
    await client.post("/api/auth/logout")
    if role == "super_admin":
        await _login_as(client, "admin", password="test-admin-pass")
    else:
        _seed_admin(username, role)
        await _login_as(client, username)

    resp = await client.get(path, headers={"Accept": "text/html"})
    assert resp.status_code == expected, (
        f"role={role} GET {path} got {resp.status_code}, expected {expected}"
    )


async def test_management_nav_omits_groups_link(client):
    """Management user lands on /admin/users; the rendered HTML's
    topbar must NOT include the Groups link."""
    _seed_admin("mgr_nav", "management")
    await client.post("/api/auth/logout")
    await _login_as(client, "mgr_nav")
    r = await client.get("/admin/users", headers={"Accept": "text/html"})
    assert r.status_code == 200
    body = r.text
    assert 'href="/admin/users"' in body
    assert 'href="/admin/groups"' not in body
    assert 'href="/admin/content"' in body   # management CAN see content
    # role is shown in the topbar badge
    assert "management" in body


async def test_content_admin_nav_omits_users_groups(client):
    """Content admin lands on /admin/broadcasts; no Users or Groups
    link in the topbar."""
    _seed_admin("ca_nav", "content_admin")
    await client.post("/api/auth/logout")
    await _login_as(client, "ca_nav")
    r = await client.get("/admin/broadcasts", headers={"Accept": "text/html"})
    assert r.status_code == 200
    body = r.text
    assert 'href="/admin/users"' not in body
    assert 'href="/admin/groups"' not in body
    assert 'href="/admin/broadcasts"' in body


async def test_unauth_redirects_to_login(client):
    """Without a session, every admin page should 303 to /admin/login."""
    await client.post("/api/auth/logout")
    r = await client.get("/admin/users", headers={"Accept": "text/html"})
    assert r.status_code == 303
    assert "/admin/login" in r.headers.get("location", "")
