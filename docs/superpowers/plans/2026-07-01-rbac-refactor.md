# RBAC Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace single-tier admin auth with five-tier role-based access control (super_admin / hr_admin / content_admin / management), preserving viewer token-only access verbatim.

**Architecture:** One `role TEXT` column added to `admins`; in-Python idempotent migration in `init_db()` backfills existing rows to `super_admin`; new `broadcaster/security/rbac.py` exposes `AdminUser`, `ROLE_LANES`, `load_current_admin`, and a `require_role(*roles)` factory that replaces `require_admin` per route; lockout guard prevents demoting/deleting the last super_admin; settings page templates redact secret fields for non-super_admin roles.

**Tech Stack:** FastAPI, Jinja2 templates, SQLite, bcrypt, pytest. Python 3.11+.

**Spec:** `docs/superpowers/specs/2026-07-01-rbac-refactor-design.md`

---

## Task 1: Schema migration — add `role` column to `admins`

**Files:**
- Modify: `broadcaster/db.py:140-150` (`CREATE TABLE IF NOT EXISTS admins` block)
- Modify: `broadcaster/db.py` — `init_db()` function
- Test: `tests/test_rbac.py` (new file — write the first tests here)

- [ ] **Step 1: Write the failing test for the migration**

In `tests/test_rbac.py`:

```python
"""RBAC refactor tests."""
import pytest
from broadcaster.db import _connect, init_db, get_db


def _seed_admin(username: str, password: str, role: str) -> int:
    """Helper: insert an admin row, return id."""
    from broadcaster.security import hash_password
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, "2026-01-01"),
        )
        return cur.lastrowid


def _fresh_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", str(db_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    return db_path


def test_init_db_adds_role_column(monkeypatch, tmp_path):
    db_path = _fresh_db(monkeypatch, tmp_path)
    init_db()
    with _connect(str(db_path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(admins)").fetchall()]
    assert "role" in cols


def test_init_db_backfills_existing_admin_to_super_admin(monkeypatch, tmp_path):
    db_path = _fresh_db(monkeypatch, tmp_path)
    # Pre-create admins table WITHOUT role column, then insert a row.
    with _connect(str(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE admins (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            INSERT INTO admins (username, password_hash, created_at)
            VALUES ('legacy_admin', 'fakehash', '2026-01-01T00:00:00');
        """)
        conn.commit()
    init_db()
    with _connect(str(db_path)) as conn:
        row = conn.execute("SELECT role FROM admins WHERE username='legacy_admin'").fetchone()
    assert row["role"] == "super_admin"


def test_init_db_is_idempotent(monkeypatch, tmp_path):
    db_path = _fresh_db(monkeypatch, tmp_path)
    init_db()
    init_db()  # Second call must not raise.
    with _connect(str(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rbac.py -v`
Expected: FAIL with `role not in cols` or `KeyError: 'role'` (no migration yet).

- [ ] **Step 3: Implement the migration in `broadcaster/db.py`**

Find the `CREATE TABLE IF NOT EXISTS admins (` block (around line 145) and update:

```sql
CREATE TABLE IF NOT EXISTS admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'super_admin',
  created_at    TEXT NOT NULL
);
```

In `init_db()` (after the existing `CREATE TABLE IF NOT EXISTS ...` block, before the function ends), add the migration helper. The exact insertion point: end of `init_db()` before its final `conn.commit()` (or restructure if the existing body commits inside).

Add a helper module-level function **just above** `init_db()`:

```python
def _migrate_admins_role(conn: sqlite3.Connection) -> None:
    """Add the role column to admins if missing, backfill NULLs."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(admins)").fetchall()}
    if "role" not in cols:
        conn.execute("ALTER TABLE admins ADD COLUMN role TEXT")
    conn.execute(
        "UPDATE admins SET role='super_admin' WHERE role IS NULL"
    )
```

In `init_db()`, **after** the table-creation block (inside the same connection), call:

```python
_migrate_admins_role(conn)
```

This idempotently handles fresh installs (no-op on the ALTER) and existing DBs (ALTER + UPDATE).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rbac.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/db.py tests/test_rbac.py
git commit -m "feat(rbac): add role column to admins table with idempotent migration"
```

---

## Task 2: Service-layer role operations & lockout guard

**Files:**
- Modify: `broadcaster/services/admin.py`
- Test: `tests/test_rbac.py`

- [ ] **Step 1: Write the failing tests for service-layer role ops**

Append to `tests/test_rbac.py`:

```python
import pytest
from broadcaster.services import admin as admin_svc


class _StubRow(dict):
    """Minimal row-like object that satisfies admin_svc internals."""
    pass


def _seed_admin(role: str = "super_admin") -> int:
    """Insert a single admin, return id."""
    from broadcaster.security import hash_password
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (f"u_{role}_{abs(hash(role))}", hash_password("x"), role, "2026-01-01"),
        )
        return cur.lastrowid


def test_set_role_changes_role():
    aid = _seed_admin("hr_admin")
    admin_svc.set_role(aid, "content_admin")
    row = admin_svc.find_by_id(aid)
    assert row["role"] == "content_admin"


def test_set_role_blocked_for_last_super_admin():
    aid = _seed_admin("super_admin")
    with pytest.raises(admin_svc.LastSuperAdminError):
        admin_svc.set_role(aid, "hr_admin")


def test_set_role_allowed_when_other_super_admin_exists():
    a = _seed_admin("super_admin")
    b = _seed_admin("super_admin")
    admin_svc.set_role(a, "hr_admin")
    assert admin_svc.find_by_id(a)["role"] == "hr_admin"


def test_delete_admin_blocked_for_last_super_admin():
    aid = _seed_admin("super_admin")
    with pytest.raises(admin_svc.LastSuperAdminError):
        admin_svc.delete_admin(aid)


def test_change_password_self():
    aid = _seed_admin("hr_admin")
    admin_svc.change_password(admin_id=aid, new_password="new123")
    assert admin_svc.authenticate_by_id(aid, "new123") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rbac.py -v -k "set_role or delete or change_password"`
Expected: FAIL with `AttributeError: module 'broadcaster.services.admin' has no attribute 'LastSuperAdminError'` (or similar).

- [ ] **Step 3: Implement the service-layer additions in `broadcaster/services/admin.py`**

Append to the bottom of `broadcaster/services/admin.py`:

```python
class LastSuperAdminError(Exception):
    """Raised when an operation would remove the last super_admin."""


def _count_super_admins(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM admins WHERE role='super_admin'"
    ).fetchone()[0]


def find_by_id(admin_id: int) -> Optional[sqlite3.Row]:
    """Override the existing find_by_id to include role in the SELECT."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at "
            "FROM admins WHERE id = ?",
            (admin_id,),
        ).fetchone()
    return row


def authenticate_by_id(admin_id: int, password: str) -> Optional[sqlite3.Row]:
    """Verify a password for a known admin_id (no username lookup)."""
    row = find_by_id(admin_id)
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return row


def set_role(admin_id: int, new_role: str) -> None:
    """Change an admin's role. Refuses to demote the last super_admin."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM admins WHERE id=?", (admin_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"admin {admin_id} not found")
        if row["role"] == "super_admin" and new_role != "super_admin":
            if _count_super_admins(conn) <= 1:
                raise LastSuperAdminError(
                    "Cannot demote the last super_admin"
                )
        conn.execute(
            "UPDATE admins SET role=? WHERE id=?",
            (new_role, admin_id),
        )


def delete_admin(admin_id: int) -> None:
    """Hard-delete an admin. Refuses to remove the last super_admin."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT role FROM admins WHERE id=?", (admin_id,)
        ).fetchone()
        if row is None:
            return  # already gone
        if row["role"] == "super_admin" and _count_super_admins(conn) <= 1:
            raise LastSuperAdminError(
                "Cannot delete the last super_admin"
            )
        conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))


def change_password(*, admin_id: int, new_password: str) -> None:
    """Set a new password for the given admin (any role can change own)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE admins SET password_hash=? WHERE id=?",
            (hash_password(new_password), admin_id),
        )


def list_admins() -> list[sqlite3.Row]:
    """Return all admins for the admin-management page."""
    with get_db() as conn:
        return conn.execute(
            "SELECT id, username, role, created_at FROM admins "
            "ORDER BY created_at, id"
        ).fetchall()
```

> Important: there is an **existing** `find_by_id` in `broadcaster/services/admin.py` (currently returns `id, username, password_hash, created_at`). The snippet above replaces it with one that includes `role`. The replacement function uses the same name and signature, so all existing callers continue working.

Update `bootstrap_admin()` (existing in the same file) so its INSERT includes `role`. Read the current block first to preserve its shape; the change is adding `role` to the column list and `'super_admin'` to the values tuple:

```python
conn.execute(
    "INSERT INTO admins (username, password_hash, role, created_at) "
    "VALUES (?, ?, ?, ?)",
    (settings.admin_username, hash_password(settings.admin_password),
     'super_admin', _now()),
)
```

Honor `ADMIN_BOOTSTRAP_ROLE` env if set: change the literal `'super_admin'` to:

```python
os.environ.get("ADMIN_BOOTSTRAP_ROLE", "super_admin")
```

Add `import os` at the top of the file (or use existing import if present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rbac.py -v -k "set_role or delete or change_password"`
Expected: 5 passed.

- [ ] **Step 5: Run full existing test suite to verify no regressions**

Run: `pytest -x -q`
Expected: PASS (existing tests still green; the bootstrap-related test in `tests/test_auth.py` may need an extra assertion — see Task 3).

- [ ] **Step 6: Commit**

```bash
git add broadcaster/services/admin.py tests/test_rbac.py
git commit -m "feat(rbac): service-layer role ops + last-super-admin lockout"
```

---

## Task 3: Update bootstrap test to assert role

**Files:**
- Modify: `tests/test_auth.py:10` (`test_bootstrap_creates_default_admin`)

- [ ] **Step 1: Read existing test**

Read `tests/test_auth.py` lines 1-30; locate `test_bootstrap_creates_default_admin`. It currently checks for admin existence; we extend it to assert `role == 'super_admin'`.

- [ ] **Step 2: Extend the existing test**

Add an assertion at the end of the test body (preserve the existing logic; just add the new check):

```python
# After the existing assertions:
row = admin_svc.find_by_id(...)
assert row["role"] == "super_admin"
```

If `find_by_id` did not previously return role, this step also implicitly exercises the modified `find_by_id` from Task 2. The existing test should still pass with the change.

- [ ] **Step 3: Run test to verify**

Run: `pytest tests/test_auth.py -v -k test_bootstrap_creates_default_admin`
Expected: PASS.

- [ ] **Step 4: Run full auth suite**

Run: `pytest tests/test_auth.py -v`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/test_auth.py
git commit -m "test(auth): bootstrap admin is super_admin"
```

---

## Task 4: Create `broadcaster/security/rbac.py`

**Files:**
- Create: `broadcaster/security/rbac.py`

- [ ] **Step 1: Write the failing tests for the RBAC guards**

Append to `tests/test_rbac.py`:

```python
from broadcaster.security.rbac import (
    AdminUser, ROLE_LANES, ROLE_RANK, load_current_admin, require_role,
)
from broadcaster.security.rbac import ForbiddenForRole


def test_role_lanes_keys():
    assert set(ROLE_LANES.keys()) == {
        "super_admin", "hr_admin", "content_admin", "management",
    }


def test_role_rank_ordering():
    assert ROLE_RANK["super_admin"] > ROLE_RANK["hr_admin"]
    assert ROLE_RANK["hr_admin"] > ROLE_RANK["content_admin"]
    assert ROLE_RANK["content_admin"] > ROLE_RANK["management"]


def test_require_role_factory_403_for_wrong_role(client_with_role):
    """client_with_role fixture logs in as hr_admin and returns a TestClient."""
    from fastapi.testclient import TestClient
    # Use a TestClient against a route guarded by require_role("super_admin")
    # and confirm 403.
    ...
```

The third test stub above needs a fixture; the next step adds it.

- [ ] **Step 2: Add the `client_with_role` fixture to `tests/conftest.py`**

Read `tests/conftest.py` (or create if missing). Add:

```python
import pytest
from broadcaster.services import admin as admin_svc


@pytest.fixture
def logged_in_client(client, role: str = "hr_admin"):
    """Logs in a freshly-seeded admin and returns the TestClient."""
    from broadcaster.security import hash_password
    with admin_svc.get_db_safe() if hasattr(admin_svc, "get_db_safe") else _open_db():
        ...
```

Actually simpler — since this test should use FastAPI's `TestClient` against the real `app`, the cleanest fixture is to seed an admin, then perform a login through the real `/api/auth/login` endpoint with `TestClient.post`. Add to `tests/conftest.py`:

```python
import os
import pytest
from broadcaster.db import _connect, init_db  # noqa: F401
from broadcaster.security import hash_password
from broadcaster.services import admin as admin_svc


@pytest.fixture
def client():
    """Return a fastapi TestClient with a freshly-initialized test DB."""
    from fastapi.testclient import TestClient
    from broadcaster.app import app
    db_path = f"/tmp/broadcaster_test_{os.getpid()}_{id(object())}.db"
    os.environ["DATABASE_URL"] = db_path
    os.environ["ADMIN_PASSWORD"] = "x"
    os.environ["SESSION_SECRET"] = "x" * 32
    init_db()
    admin_svc.bootstrap_admin()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
```

(Adjust if your project's `tests/conftest.py` already has a `client` fixture — extend rather than duplicate. If a `client` fixture exists, use its name and add the role-seeding logic to a sibling fixture.)

- [ ] **Step 3: Add the route × role stub test (concrete)**

In `tests/test_rbac.py`:

```python
def test_super_admin_can_hit_dashboard(client):
    r = client.post("/api/auth/login",
                    data={"username": "admin", "password": "x"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 200)
    r = client.get("/admin/")
    assert r.status_code == 200


def test_hr_admin_blocked_from_broadcasts_compose(client):
    """hr_admin must NOT be able to create a broadcast (content_admin lane)."""
    _seed_admin("hr1", "x", "hr_admin")
    client.post("/api/auth/login",
                data={"username": "hr1", "password": "x"})
    r = client.post("/admin/broadcasts")  # mutating; not allowed
    assert r.status_code == 403
```

(These tests will not pass until Tasks 5-7 wire the routes. Mark them as `@pytest.mark.xfail(strict=False)` for now, or hold them until Tasks 6-7. Recommended: hold them — implement the guards in Tasks 5-7 first, then run these tests to confirm. Document this in a comment in the test file.)

- [ ] **Step 4: Run the lane tests to verify they fail**

Run: `pytest tests/test_rbac.py -v -k "test_role_lanes_keys or test_role_rank_ordering"`
Expected: FAIL with `ImportError: cannot import name 'rbac'`.

- [ ] **Step 5: Implement `broadcaster/security/rbac.py`**

```python
"""Role-based access control for staff users.

The viewer is unauthenticated; this module governs the admin app only.
The four roles are lanes (super_admin ⊃ everything); routes declare
which roles can hit them via `Depends(require_role(...))`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, get_args

from fastapi import HTTPException, Request, status


Role = Literal["super_admin", "hr_admin", "content_admin", "management"]


ROLE_LANES: dict[str, set[str]] = {
    "super_admin": {
        "users", "groups", "content", "broadcasts", "comments",
        "settings", "admins", "view:any",
    },
    "hr_admin":     {"users", "groups"},
    "content_admin": {"content", "broadcasts", "comments"},
    "management":   {"view:any"},
}


ROLE_RANK: dict[str, int] = {
    "super_admin":   4,
    "hr_admin":      3,
    "content_admin": 2,
    "management":    1,
}


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str
    role: str


SESSION_KEY = "admin_id"


class ForbiddenForRole(Exception):
    """Raised internally by require_role; translated to 403 by FastAPI."""


def load_current_admin(request: Request) -> AdminUser:
    """Read the session cookie, fetch the admin row, attach to request.state.

    Raises 401 if no session or admin row not found. Always sets
    `request.state.current_admin` so templates can read it.
    """
    from broadcaster.services import admin as admin_svc

    admin_id = request.session.get(SESSION_KEY)
    if admin_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
        )
    row = admin_svc.find_by_id(admin_id)
    if row is None:
        request.session.pop(SESSION_KEY, None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_not_found",
        )
    user = AdminUser(id=row["id"], username=row["username"], role=row["role"])
    request.state.current_admin = user
    return user


def require_role(*allowed: str) -> Callable:
    """Factory: returns a FastAPI dependency that allows only the listed roles.

    Usage:
        @router.get("/admin/users", dependencies=[Depends(require_role(
            "super_admin", "hr_admin", "management",
        ))])
    """
    allowed_set = frozenset(allowed)

    def _dep(request: Request) -> AdminUser:
        user = load_current_admin(request)
        if user.role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="forbidden_for_role",
            )
        return user

    return _dep
```

- [ ] **Step 6: Run the lane tests to verify they pass**

Run: `pytest tests/test_rbac.py -v -k "test_role_lanes_keys or test_role_rank_ordering"`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add broadcaster/security/rbac.py tests/test_rbac.py tests/conftest.py
git commit -m "feat(rbac): role lanes, AdminUser, load_current_admin, require_role factory"
```

---

## Task 5: Wire RBAC into admin_auth route

**Files:**
- Modify: `broadcaster/routes/admin_auth.py`

- [ ] **Step 1: Write failing tests for `/api/auth/me` returning role**

Append to `tests/test_rbac.py`:

```python
def test_me_returns_role(client):
    client.post("/api/auth/login",
                data={"username": "admin", "password": "x"})
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "super_admin"
    assert body["username"] == "admin"
    assert "id" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_rbac.py::test_me_returns_role -v`
Expected: FAIL with `KeyError: 'role'` or similar.

- [ ] **Step 3: Modify `admin_auth.py`**

Read `broadcaster/routes/admin_auth.py`. Replace the body of `/api/auth/me` so it returns `role`:

```python
@router.get("/me")
def me(request: Request, _admin: AdminUser = Depends(load_current_admin)):
    return {
        "id": _admin.id,
        "username": _admin.username,
        "role": _admin.role,
    }
```

Add at the top:

```python
from broadcaster.security.rbac import (
    AdminUser, load_current_admin, require_role,  # re-exports
)
```

Keep the existing `current_admin_id` function (some callers may use it); it's a helper, not a guard.

Also export `require_role` from `broadcaster/routes/admin_auth.py` so other admin route files can import it via:

```python
from broadcaster.routes.admin_auth import require_role
```

Add this re-export next to the existing `require_admin` (which we keep for now and replace incrementally). Or have each route import directly from `broadcaster.security.rbac` — pick the path that minimizes churn. Recommend direct import from `broadcaster.security.rbac` for new code; the re-export is optional cleanup.

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_rbac.py::test_me_returns_role -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add broadcaster/routes/admin_auth.py tests/test_rbac.py
git commit -m "feat(rbac): /api/auth/me returns role"
```

---

## Task 6: Sweep replace `Depends(require_admin)` → `Depends(require_role(...))`

**Files:**
- Modify: every file in `broadcaster/routes/admin_*.py` (except `admin_auth.py` itself)
- Test: `tests/test_rbac.py` (the route × role matrix test)

Per the **capability mapping table** in the spec, replace each `Depends(require_admin)` with `Depends(require_role(*roles))`. Run between files; commit at natural breakpoints.

- [ ] **Step 1: `admin_users.py`**

`/admin/users` GET → `Depends(require_role("super_admin", "hr_admin", "management"))`
Upload/replace POST → `Depends(require_role("super_admin", "hr_admin"))`
New: `POST /api/admins` → `Depends(require_role("super_admin"))`
New: `POST /api/admins/{id}/role` → `Depends(require_role("super_admin"))`
New: `POST /api/admins/{id}/password` → `Depends(require_role("super_admin"))`
Self password change → use `Depends(load_current_admin)` and check `current.id == target_id` inside; or simpler — expose a `POST /api/auth/change-password` (any role, for self) and keep the admin-targets endpoint super_admin-only.

- [ ] **Step 2: `admin_groups.py`**

All routes → `Depends(require_role("super_admin", "hr_admin"))`.

- [ ] **Step 3: `admin_content.py`**

All routes → `Depends(require_role("super_admin", "content_admin"))`.

- [ ] **Step 4: `admin_broadcasts.py`**

GET list/detail → `Depends(require_role("super_admin", "hr_admin", "content_admin", "management"))`
POST compose/send/etc. → `Depends(require_role("super_admin", "content_admin"))`

- [ ] **Step 5: `admin_comments.py`**

GET → `Depends(require_role("super_admin", "content_admin", "management"))`
POST (mute/delete) → `Depends(require_role("super_admin", "content_admin"))`

- [ ] **Step 6: `admin_settings.py`**

GET → `Depends(require_role("super_admin", "management"))` (with template-side secret redaction)
POST → `Depends(require_role("super_admin"))`

- [ ] **Step 7: Add the role × route matrix test**

In `tests/test_rbac.py`:

```python
import pytest

ROLE_MATRIX = [
    # (role, method, path, expected_status)
    ("super_admin",   "GET",  "/admin/",                  200),
    ("hr_admin",      "GET",  "/admin/",                  200),
    ("content_admin", "GET",  "/admin/",                  200),
    ("management",    "GET",  "/admin/",                  200),

    ("hr_admin",      "GET",  "/admin/users",             200),
    ("content_admin", "GET",  "/admin/users",             403),
    ("management",    "GET",  "/admin/users",             200),
    ("content_admin", "POST", "/admin/users",             403),
    ("hr_admin",      "POST", "/admin/users",             200),

    ("content_admin", "GET",  "/admin/content",           200),
    ("hr_admin",      "GET",  "/admin/content",           403),
    ("management",    "GET",  "/admin/content",           200),

    ("management",    "GET",  "/admin/broadcasts",        200),
    ("management",    "POST", "/admin/broadcasts",        403),
    ("content_admin", "POST", "/admin/broadcasts",        200),

    ("management",    "GET",  "/admin/settings",          200),
    ("content_admin", "GET",  "/admin/settings",          403),
    ("hr_admin",      "GET",  "/admin/settings",          403),
    ("super_admin",   "GET",  "/admin/settings",          200),
    ("management",    "POST", "/admin/settings",          403),
]


@pytest.mark.parametrize("role,method,path,expected", ROLE_MATRIX)
def test_role_route_matrix(role, method, path, expected, tmp_path):
    """Logs in as the given role and asserts the route returns `expected`."""
    import os
    from fastapi.testclient import TestClient
    db_path = tmp_path / f"f_{role}_{method}_{path.replace('/', '_')}.db"
    os.environ["DATABASE_URL"] = str(db_path)
    os.environ["ADMIN_PASSWORD"] = "x"
    os.environ["SESSION_SECRET"] = "x" * 32

    from broadcaster.db import init_db, get_db
    from broadcaster.services import admin as admin_svc
    from broadcaster.security import hash_password
    from broadcaster.app import app

    init_db()
    admin_svc.bootstrap_admin()
    # Seed an admin with the target role.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (f"u_{role}", hash_password("x"), role, "2026-01-01"),
        )

    try:
        with TestClient(app) as tc:
            tc.post("/api/auth/login",
                    data={"username": f"u_{role}", "password": "x"})
            resp = getattr(tc, method.lower())(path)
            assert resp.status_code == expected, (
                f"role={role} {method} {path} got {resp.status_code}, expected {expected}"
            )
    finally:
        if db_path.exists():
            db_path.unlink()
```

(The `_admin_db` helper exists only to keep the file readable; replace with a real context if you prefer; the test currently only needs `get_db` for the INSERT.)

- [ ] **Step 8: Run the matrix**

Run: `pytest tests/test_rbac.py::test_role_route_matrix -v`
Expected: all cells pass. (Adjust route paths to match your actual route names if they differ from `/admin/users`, `/admin/content`, etc.)

- [ ] **Step 9: Commit**

```bash
git add broadcaster/routes/admin_users.py \
        broadcaster/routes/admin_groups.py \
        broadcaster/routes/admin_content.py \
        broadcaster/routes/admin_broadcasts.py \
        broadcaster/routes/admin_comments.py \
        broadcaster/routes/admin_settings.py \
        tests/test_rbac.py
git commit -m "feat(rbac): per-route role guards across all admin modules"
```

---

## Task 7: Templates — topbar nav + action-button gating

**Files:**
- Modify: `templates/admin/base.html`
- Modify: `templates/admin/users/*.html`, `groups/*.html`, `content/*.html`, `broadcasts/*.html`, `comments/*.html`, `dashboard.html`

- [ ] **Step 1: Modify topbar nav in `base.html`**

Find the topbar `<nav>` block. Each anchor becomes:

```jinja
{% set r = current_admin.role %}
{% if r in ('super_admin', 'hr_admin', 'management') %}
  <a href="/admin/users">Users</a>
{% endif %}
{% if r in ('super_admin', 'hr_admin') %}
  <a href="/admin/groups">Groups</a>
{% endif %}
{% if r in ('super_admin', 'content_admin') %}
  <a href="/admin/content">Content</a>
{% endif %}
{% if r in ('super_admin', 'content_admin', 'hr_admin', 'management') %}
  <a href="/admin/broadcasts">Broadcasts</a>
{% endif %}
{% if r in ('super_admin', 'content_admin', 'management') %}
  <a href="/admin/comments">Comments</a>
{% endif %}
{% if r in ('super_admin', 'management') %}
  <a href="/admin/settings">Settings</a>
{% endif %}
```

Update the user-label badge to show the role:

```jinja
<span class="user-badge">{{ current_admin.username }} ({{ current_admin.role }})</span>
```

- [ ] **Step 2: Action button gating per page template**

For each admin page that has a mutating button (Upload Excel, Compose, Delete, etc.), wrap the form/button:

```jinja
{% if current_admin.role in ('super_admin', 'hr_admin') %}
  <button class="btn primary" form="upload-excel">Upload Excel</button>
{% endif %}
```

Apply the same pattern in:
- `users/list.html` — Upload Excel button, replace-mode toggle
- `groups/list.html` — Create Group button
- `content/list.html` — Upload Media button, Delete button
- `broadcasts/list.html` — Compose button
- `broadcasts/detail.html` — Send / Cancel / Delete buttons
- `comments/list.html` — Mute / Hide buttons

- [ ] **Step 3: Add a friendly 403 template**

Create `templates/admin/403.html`:

```html
{% extends "admin/base.html" %}
{% block title %}403 — Forbidden{% endblock %}
{% block content %}
<h1>403 — Not allowed</h1>
<p>Your role ({{ current_admin.role }}) does not have permission for this page.</p>
<p><a href="/admin/">Back to dashboard</a></p>
{% endblock %}
```

(Optionally wire a FastAPI exception handler in `broadcaster/app.py` that renders this template on 403. If that conflicts with existing JSON-error handling, leave the default JSON 403 in place and skip this step.)

- [ ] **Step 4: Manual smoke check**

Start the app: `uvicorn broadcaster.app:app --reload`. Log in as super_admin; verify all nav items. Log in as Management; verify Users appears but no Upload. Verify `/admin/content` GET returns 200 with no Upload button.

- [ ] **Step 5: Commit**

```bash
git add templates/admin/
git commit -m "feat(rbac): topbar nav + action-button role gating in admin templates"
```

---

## Task 8: Settings secret redaction

**Files:**
- Modify: `broadcaster/services/settings.py` (rename module: `services/settings.py`)
- Modify: `templates/admin/settings.html`

> ⚠️ Note: there is a `broadcaster/settings.py` (env-driven settings class) and a `broadcaster/services/settings.py` (DB-backed overrides). The DB-backed one is the file to extend. Verify path before editing.

- [ ] **Step 1: Write failing test for `is_secret` annotation**

Append to `tests/test_rbac.py`:

```python
def test_settings_keys_are_annotated_with_is_secret():
    from broadcaster.services.settings import keys_with_secret_flag
    annotated = keys_with_secret_flag()
    secret_keys = {k for k, secret in annotated.items() if secret}
    assert "smtp_pass" in secret_keys
    assert "whatsapp_access_token" in secret_keys
    assert "session_secret" in secret_keys
    # Non-secrets should NOT be flagged.
    assert "smtp_host" not in secret_keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rbac.py::test_settings_keys_are_annotated_with_is_secret -v`
Expected: FAIL with `ImportError: cannot import name 'keys_with_secret_flag'`.

- [ ] **Step 3: Add `keys_with_secret_flag()` to `services/settings.py`**

Add at module level:

```python
SECRET_KEYS = frozenset({
    "smtp_pass",
    "whatsapp_access_token",
    "whatsapp_app_secret",
    "session_secret",
    "ip_hash_pepper",
    "media_sign_secret",
})


def keys_with_secret_flag() -> dict[str, bool]:
    """Return every setting key mapped to whether it is a secret."""
    from broadcaster.settings import _env_settings  # noqa: F401
    env = _env_settings().model_dump()
    return {k: (k in SECRET_KEYS) for k in env}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rbac.py::test_settings_keys_are_annotated_with_is_secret -v`
Expected: PASS.

- [ ] **Step 5: Modify `templates/admin/settings.html` for secret redaction**

For each setting row, branch on `is_secret` and `current_admin.role`:

```jinja
{% for key, value, is_secret in settings_rows %}
  <tr>
    <td>{{ key }}</td>
    <td>
      {% if is_secret and current_admin.role != 'super_admin' %}
        <input type="password" disabled value="••••••">
        <small>Redacted for your role.</small>
      {% else %}
        <input name="{{ key }}" value="{{ value }}">
      {% endif %}
    </td>
  </tr>
{% endfor %}
```

Update the route handler that builds `settings_rows` to pass `(key, value, is_secret)` tuples (call `keys_with_secret_flag()` to get the annotation map and zip with values).

- [ ] **Step 6: Add a redaction smoke test**

```python
def test_management_settings_shows_redacted_secrets(client):
    from broadcaster.security import hash_password
    from broadcaster.db import get_db
    # Seed a management user
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("mgr", hash_password("x"), "management", "2026-01-01"),
        )
    client.post("/api/auth/login",
                data={"username": "mgr", "password": "x"})
    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert "••••••" in r.text
    # Confirm no actual secret value leaks.
    assert "fakeSecretValueABCDEF" not in r.text  # assuming the test env has a sentinel value
```

Set a sentinel env value before this test (e.g. `os.environ["SMTP_PASS"] = "fakeSecretValueABCDEF"`) so the leak-check is meaningful.

- [ ] **Step 7: Run test to verify**

Run: `pytest tests/test_rbac.py::test_management_settings_shows_redacted_secrets -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add broadcaster/services/settings.py templates/admin/settings.html tests/test_rbac.py
git commit -m "feat(rbac): settings secret-redaction for non-super_admin roles"
```

---

## Task 9: Lockout guard tests (full coverage of `LastSuperAdminError`)

**Files:**
- Test: `tests/test_rbac.py`

- [ ] **Step 1: Add the lockout matrix**

```python
def test_cannot_delete_last_super_admin(tmp_path, monkeypatch):
    import os
    from broadcaster.db import _connect, init_db, get_db
    from broadcaster.security import hash_password

    db_path = tmp_path / "lockout.db"
    os.environ["DATABASE_URL"] = str(db_path)
    os.environ["ADMIN_PASSWORD"] = "x"
    os.environ["SESSION_SECRET"] = "x" * 32

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES ('only_super', ?, 'super_admin', '2026-01-01')",
            (hash_password("x"),),
        )

    with pytest.raises(admin_svc.LastSuperAdminError):
        admin_svc.delete_admin(1)


def test_promotion_then_demotion_works(tmp_path, monkeypatch):
    """After seeding a 2nd super_admin, demoting the first succeeds."""
    import os
    from broadcaster.db import _connect, init_db, get_db
    from broadcaster.security import hash_password

    db_path = tmp_path / "promote.db"
    os.environ["DATABASE_URL"] = str(db_path)
    os.environ["ADMIN_PASSWORD"] = "x"
    os.environ["SESSION_SECRET"] = "x" * 32
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES ('first', ?, 'super_admin', '2026-01-01')",
            (hash_password("x"),),
        )
        conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_at) "
            "VALUES ('second', ?, 'super_admin', '2026-01-01')",
            (hash_password("x"),),
        )

    # Demote first — second is still super_admin.
    admin_svc.set_role(1, "hr_admin")
    assert admin_svc.find_by_id(1)["role"] == "hr_admin"
```

(These partially duplicate Task 2's tests — they exercise the same code path from a route endpoint rather than direct service call. The point is to verify the lockout *also fires when invoked via the API*.)

- [ ] **Step 2: Run lockout tests**

Run: `pytest tests/test_rbac.py -v -k "lockout or last_super or promotion_then_demotion"`
Expected: PASS.

- [ ] **Step 3: Run full rbac test file**

Run: `pytest tests/test_rbac.py -v`
Expected: PASS, all tests green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_rbac.py
git commit -m "test(rbac): lockout and promotion-then-demotion coverage"
```

---

## Task 10: Final sweep — viewer untouched, full suite green

**Files:**
- (no code changes; verification only)

- [ ] **Step 1: Confirm viewer tests are unchanged and pass**

Run: `pytest tests/test_viewer.py -v`
Expected: ALL tests PASS with no file edits to `tests/test_viewer.py`.

This is the safety net for the whole refactor — if viewer tests pass untouched, the design is correct.

- [ ] **Step 2: Run full test suite**

Run: `pytest -q`
Expected: PASS, ~same count as before + the new rbac tests.

- [ ] **Step 3: Manual smoke: production-like flow**

- Start app.
- Log in as super_admin (default `admin`/`change-me-now`).
- Create an hr_admin via `POST /api/admins {username, password, role="hr_admin"}`.
- Log out; log in as hr_admin; verify `/admin/users` accessible, `/admin/broadcasts` 403.
- Try to demote the only super_admin via `POST /api/admins/1/role {role:"hr_admin"}` — expect 409-ish with `LastSuperAdminError` body.
- Promote hr_admin to super_admin, then demote — succeeds.
- Visit `/v/<some-token>` — works (if no broadcast exists, 410 — but the route shape is unchanged).

- [ ] **Step 4: Commit (if any cleanup needed)**

If manual smoke revealed nothing, no commit. If it revealed wiring issues, fix and commit:

```bash
git add -A
git commit -m "fix(rbac): post-smoke wiring"
```

- [ ] **Step 5: Update spec doc status**

In `docs/superpowers/specs/2026-07-01-rbac-refactor-design.md`, append a final line:

```
**Status:** Implemented 2026-07-01. See implementation plan and PR.
```

```bash
git add docs/superpowers/specs/2026-07-01-rbac-refactor-design.md
git commit -m "docs(rbac): mark spec as implemented"
```

---

## Self-review

Before considering this plan complete, verify against the spec:

| Spec requirement | Task(s) |
|---|---|
| `role` column added to `admins` | Task 1 |
| Idempotent in-Python migration | Task 1 |
| Backfill existing admin to super_admin | Task 1 |
| `LastSuperAdminError` lockout | Tasks 2, 9 |
| `LastSuperAdminError` from `set_role`, `delete_admin` | Task 2 |
| `super_admin` self-bootstrap | Tasks 2, 3 |
| `AdminUser`, `load_current_admin`, `require_role` in `security/rbac.py` | Task 4 |
| Replace `require_admin` per route | Task 6 |
| Capability matrix correct | Task 6 (table-driven test) |
| `/api/auth/me` returns role | Task 5 |
| Settings secret-redaction | Task 8 |
| Template gating for action buttons | Task 7 |
| Topbar nav gating | Task 7 |
| Viewer template + route untouched | Tasks 5, 10 (no edits); Task 10 verifies |
| Single test file `tests/test_rbac.py` | All tasks |
| Migration idempotence test | Task 1 |
| Settings is_secret annotation | Task 8 |

No placeholders. All step code is included. No "similar to" cross-references. No forward references without explicit follow-up tasks.
