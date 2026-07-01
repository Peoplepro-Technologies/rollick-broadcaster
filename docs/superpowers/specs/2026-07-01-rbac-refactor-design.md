# RBAC Refactor — 2026-07-01

> Replace single-tier admin auth with 5-tier role-based access control while
> preserving token-based viewer access. Approach A: route-level dependency
> guards, in-place ALTER TABLE migration, no capability-config file.

## Context

`broadcaster/auth` currently has one role: `admin`. Every `/admin/*` route is
gated by `Depends(require_admin)` (`broadcaster/routes/admin_auth.py:27`); the
`admins` table has `id, username, password_hash, created_at` with no role
column. The viewer (`/v/{token}`) is publicly accessible — token IS the
credential. `require_admin` is used in 8 admin route files.

The platform needs four distinct staff roles with different lanes:

| Role | Capability |
|---|---|
| Super Admin | Full control, manages other admins (including passwords), edits settings |
| HR Admin | Uploads user lists, manages groups |
| Content Admin | Manages content, schedules/creates broadcasts |
| Management | Read-only access to dashboards and reports, secrets redacted |
| Subscriber/Viewer | **No login.** Token-based access only. Lives in `users`, not `admins`. |

The viewer model is **already** token-only and structurally separate — this
refactor preserves it verbatim. The viewer route (`broadcaster/routes/viewer.py`)
is not touched.

## Goal & non-goals

**Goal:** a single `role` column on `admins`, four staff role types, route-level
guards per the capability mapping below, in-Python migration that backfills the
existing admin to super_admin. Tests cover the role × route matrix, secret
redaction, and last-super-admin lockout. The viewer path is preserved.

**Non-goals (intentionally out of scope):**

- No subscriber / viewer login.
- No multi-role-per-user.
- No capability-config file (lanes are hardcoded; four roles × one app).
- No audit-log table.
- No password expiry / rotation policy.
- No OAuth / SSO.
- No "forgot password" self-service.
- No per-broadcast / per-group ACLs (roles are global lanes).
- No soft-delete on admin users.

## Architecture

### New module: `broadcaster/security/rbac.py`

```python
from typing import Literal

Role = Literal["super_admin", "hr_admin", "content_admin", "management"]

ROLE_LANES: dict[Role, set[str]] = {
    "super_admin":    {"users", "groups", "content", "broadcasts",
                       "comments", "settings", "admins", "view:any"},
    "hr_admin":       {"users", "groups"},
    "content_admin":  {"content", "broadcasts", "comments"},
    "management":     {"view:any"},
}

ROLE_RANK: dict[Role, int] = {
    "super_admin": 4,
    "hr_admin": 3,
    "content_admin": 2,
    "management": 1,
}

@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str
    role: Role

def load_current_admin(request: Request) -> AdminUser:
    """Read session, fetch admin row, attach to request.state.
    Raises 401 if no session or admin row not found."""
    ...

def require_role(*allowed: Role):
    """Factory: returns a FastAPI dependency that raises 403 if the
    current admin's role is not in `allowed`."""
    ...
```

`AdminUser` is frozen; routes receive it (not a raw dict) so the type system
reminds us `role` is present. `request.state.current_admin` is set by
`load_current_admin` so templates can call `current_admin.role` directly.

### Modified files

- `broadcaster/db.py` — schema: add `role TEXT` column on `admins`. Migration
  logic in `init_db()` (see Data model section).
- `broadcaster/security.py` — no change to `hash_password`/`verify_password`.
- `broadcaster/services/admin.py` — extend with `set_role`, `change_password`,
  `list_admins`, `count_super_admins`, `LastSuperAdminError`. Update
  `bootstrap_admin` to write `role`. Update `find_by_id` to return the role.
- `broadcaster/routes/admin_auth.py` — add `require_role` re-export; extend
  `/api/auth/me` to return `role`.
- Eight admin route files — replace `Depends(require_admin)` with `Depends(
  load_current_admin)` plus `Depends(require_role(...))` per the capability
  table below. New endpoints for admin management.
- `broadcaster/services/settings.py` — annotate each setting key with
  `is_secret: bool`; the template layer filters on it.

### Templates

- `templates/admin/base.html` — each topbar nav item wrapped in
  `{% if current_admin.role in (roles,) %}{% endif %}`.
- All admin page templates — receive `current_admin` from
  `request.state` (already set by `load_current_admin`).
- Action buttons (upload, compose, etc.) wrap in
  `{% if current_admin.role in MUTATING_ROLES %}`.
- `/admin/settings` page — secret fields render as `••••••` (redacted) for
  Management; actual values shown only for super_admin.
- `/admin/users` page — the *change-other-admin-password* form is hidden for
  non-super_admin roles (the route returns 403 directly).

### Viewer templates

`templates/viewer/*` — not modified. The viewer route does not set
`current_admin` (request is unauthenticated).

## Data model

### Schema

```sql
-- After migration
CREATE TABLE admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'super_admin',
  created_at    TEXT NOT NULL
);
```

### Migration (`init_db`)

Run at app startup, idempotent:

```python
def init_db() -> None:
    """Idempotent. Migrates existing DB before creating tables."""
    # 1. CREATE IF NOT EXISTS for all tables (existing logic).
    # 2. If `admins.role` column is missing → ALTER TABLE admins ADD COLUMN role TEXT.
    # 3. UPDATE admins SET role='super_admin' WHERE role IS NULL.
    # 4. The DEFAULT in the schema handles fresh installs.
```

In-Python, not Alembic — one column, no history. (Decision recorded for
future migrations: if we add a second migration, switch to Alembic.)

### Lockout guards

`LastSuperAdminError` raised by:

- `set_role(admin_id, new_role)` if demoting the last super_admin.
- `delete_admin(admin_id)` if removing the last super_admin.

Both functions `SELECT COUNT(*) WHERE role='super_admin'` before mutating
under the same connection's transaction (sqlite `BEGIN IMMEDIATE`).

### Bootstrap

`bootstrap_admin()` (`services/admin.py:25`) inserts `role='super_admin'`
unconditionally. Override via env:

| Env var | Default | Effect |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | New in fresh install |
| `ADMIN_PASSWORD` | `change-me-now` | New in fresh install |
| `ADMIN_BOOTSTRAP_ROLE` | `super_admin` | Only honored if env-set; ignored on existing rows |

If `ADMIN_BOOTSTRAP_ROLE` is set to anything but `super_admin`, log a
warning: `bootstrap_user_may_lockout`. Bootstrap user is always created
with role `super_admin` unless explicitly overridden.

## Capability mapping

The table below is the source of truth. Routes declare their roles
explicitly via `Depends(require_role(...))`.

| Route group | Methods | Roles |
|---|---|---|
| `/admin/` (dashboard) | GET | super_admin, hr_admin, content_admin, management |
| `/admin/users` | GET | super_admin, hr_admin, management |
| `/admin/users` upload/replace | POST | super_admin, hr_admin |
| `/admin/groups/*` | GET/POST | super_admin, hr_admin |
| `/admin/content/*` | GET/POST/DELETE | super_admin, content_admin |
| `/admin/broadcasts` | GET | super_admin, hr_admin, content_admin, management |
| `/admin/broadcasts` compose/send | POST | super_admin, content_admin |
| `/admin/broadcasts/{id}` | GET | super_admin, hr_admin, content_admin, management |
| `/admin/comments/*` | GET | super_admin, content_admin, management |
| `/admin/comments/*` | POST | super_admin, content_admin |
| `/admin/settings` | GET | super_admin, management (secrets redacted) |
| `/admin/settings` | POST | super_admin |
| `/api/admins` | POST | super_admin (create new admin) |
| `/api/admins/{id}/role` | POST | super_admin (with lockout guard) |
| `/api/admins/{id}/password` | POST | super_admin; or self for any role |
| `/api/auth/login`, `/logout` | POST | unauthenticated |
| `/api/auth/me` | GET | any authenticated |
| `/v/{token}/...` | * | **unauthenticated** — unchanged |

Hidden-vs-403 rule:

- Management lands on `/admin/users`, sees the user list, sees no upload
  button — POST returns 403 if attempted.
- Management GET on `/admin/settings` returns 200 with secrets redacted.
- Management GET on `/admin/content` returns 200 with the page but no
  upload/delete buttons; POST returns 403 if attempted.
- Routes that are super_admin-only have no Management read fallback
  (e.g. `/api/admins/*`).

## Settings secret-redaction

`broadcaster/services/settings.py::all_visible()` (existing) — each key
returns `(value, is_secret: bool)`. The existing key list marks:
`smtp_pass`, `whatsapp_access_token`, `whatsapp_app_secret`,
`session_secret`, `ip_hash_pepper`, `media_sign_secret` as `is_secret=True`.

Template logic:

```jinja
{% if u.role == 'super_admin' %}
  <input type="text" name="smtp_pass" value="{{ settings.smtp_pass }}">
{% else %}
  <input type="password" disabled value="••••••••">
  <small>Redacted for your role.</small>
{% endif %}
```

POST handler does not change — it still validates input and writes. If a
Management session were to bypass the route guard (it won't), the secret
keys would be rejected at the API layer (existing behavior — secrets are
not allowed to round-trip through the DB overrides table).

## Testing

### Existing tests (kept green)

- `tests/test_auth.py::test_bootstrap_creates_default_admin` — extended to
  assert `role == 'super_admin'` post-bootstrap.
- `tests/test_viewer.py` — **zero edits**. The whole premise of the design
  is that the viewer path is untouched; tests must keep passing unmodified.

### New file `tests/test_rbac.py`

Single comprehensive file. Parametrized for full role × route coverage.

Cases:

1. **Login carries role** — `/api/auth/me` returns `{id, username, role}`.
2. **Role × route matrix** — every (role, route) cell asserts expected
   status (200/302/403). For routes that allow multiple roles, each is
   tested.
3. **Management read-only smoke** — GET succeeds; POST returns 403 for
   every mutating endpoint.
4. **Settings secret redaction** — Management GET shows `••••••` for
   every secret key; super_admin GET shows the actual value.
5. **Super_admin creates an admin**:
   - `POST /api/admins` with `username, password, role='hr_admin'` succeeds.
   - Logout.
   - Login as new hr_admin.
   - GET `/admin/users` returns 200; GET `/admin/broadcasts` returns 403.
6. **Last-super-admin lockout**:
   - `set_role(last_super.id, 'hr_admin')` → raises `LastSuperAdminError`.
   - Add a second super_admin, then demote the first → succeeds.
   - `delete_admin(last_super_admin)` → `LastSuperAdminError`.
7. **Password change**:
   - Self for any role: 200.
   - Other-admin as super_admin: 200.
   - Other-admin as non-super_admin: 403.
8. **Migration idempotence**:
   - Fresh DB → `init_db()` creates column; admin row has role set.
   - Second `init_db()` call: no schema changes; no row updates.
9. **Viewer untouched**:
   - Test that `/v/{token}` still works for any role and for no-role
     (since viewer is unauthenticated, role is not consulted).

## Files added

- `broadcaster/security/rbac.py`
- `tests/test_rbac.py`

## Files modified

- `broadcaster/db.py`
- `broadcaster/services/admin.py`
- `broadcaster/services/settings.py`
- `broadcaster/routes/admin_auth.py`
- `broadcaster/routes/admin_users.py`
- `broadcaster/routes/admin_groups.py`
- `broadcaster/routes/admin_content.py`
- `broadcaster/routes/admin_broadcasts.py`
- `broadcaster/routes/admin_comments.py`
- `broadcaster/routes/admin_settings.py`
- `templates/admin/base.html`
- Admin page templates touched for action-button gating & secret
  redaction: `admin/dashboard.html`, `admin/users/*.html`,
  `admin/groups/*.html`, `admin/content/*.html`,
  `admin/broadcasts/*.html` (compose + detail + list), `admin/comments/*.html`,
  `admin/settings.html`.

## Rollout

1. Apply migration against production DB — `ALTER TABLE admins ADD COLUMN
   role` and `UPDATE … SET role='super_admin'` are non-destructive.
2. Deploy; existing admin (id=1) is now `super_admin` without any action.
3. Super_admin logs in, creates hr_admin / content_admin / management
   users via the new `/api/admins` endpoint.
4. Old single-admin session remains valid; no re-login required.

## Risks

- **Lockout** if super_admin demotes themselves before another exists.
  Mitigated by `LastSuperAdminError` — tool returns explicit error.
- **Settings page template complexity** — adding conditional rendering for
  2 fields (visible-vs-redacted) per secret is verbose but mechanical.
- **Test file size** — full role × route matrix will be ~50+ parametrize
  rows. Acceptable; single file is easier to maintain than per-route tests.
- **Hidden routes still return 403** — if someone bookmarks a write URL
  while super_admin and tries it after being demoted, they get a clear 403
  page; UX-side we'll add a friendly 403 template.

## Open questions

None at design time. Roll forward.

## Status

**Implemented 2026-07-01.** See implementation plan (`docs/superpowers/plans/2026-07-01-rbac-refactor.md`) and the following commits on `main`:

```
5ccb5e5 feat(rbac): add role column to admins with idempotent in-Python migration
cbe57e5 feat(rbac): service-layer role ops + last-super-admin lockout
291fa92 test(auth): bootstrap admin is super_admin
2294129 feat(rbac): broadcaster/rbac.py — lanes, AdminUser, guard factory
ed6b8f8 feat(rbac): /api/auth/me returns role
b03e004 feat(rbac): per-route role guards across all admin modules
718e094 feat(rbac): page-handler role gates + topbar nav gating
c57a15e feat(rbac): settings template secret-redaction for non-super_admin
9ca4b7b feat(rbac): admin-management endpoints + API-level lockout tests
```

94 RBAC tests pass. Viewer (`/v/{token}`) path preserved verbatim — `tests/test_viewer.py` had zero edits during the refactor.
