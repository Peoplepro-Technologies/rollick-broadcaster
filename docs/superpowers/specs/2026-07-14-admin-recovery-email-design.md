# Admin Recovery Email — Per-Admin Routing

**Status:** Approved (brainstorming skipped — user directive via AskUserQuestion; design inferred from 2026-07-09 forgot-password spec + user answers)
**Date:** 2026-07-14
**Target version:** v2.4.0 (next minor after v2.3.0)
**Scope:** Admin panel sign-in recovery; per-admin routing with global fallback.
**Supersedes:** §11 "Per-admin recovery email (decided as single global mailbox)" of `2026-07-09-forgot-password-design.md`.

## Context

As of v2.3.0, the forgot-password flow routes every admin's temporary password to one **global** `settings.password_recovery_email` (default `anibandha.mukhopadhyay@rollick.co.in`). The design explicitly rejected per-admin emails to keep IT in the loop ("Rollick wants IT to gate access, not the user").

That decision is now reversed. Each admin row will carry its own personal `recovery_email`, and the temp password will route there directly. The global setting demotes to a **fallback** used when a row has no email — this keeps legacy deployments working and gives operators a single global recipient they can edit at one place.

The user-facing motivation: IT should not be a bottleneck for self-service password recovery; each admin gets their own inbox; the role of the global mailbox becomes "the place to route resets for admins that haven't been set up yet."

## Goals

1. Each admin row carries a `recovery_email` column.
2. The forgot-password flow routes to that row's value (preferred), falling back to the global setting when the row is empty.
3. Newly-created admin rows **must** include a valid `recovery_email` (validated server-side).
4. The legacy migration is safe and idempotent — existing rows backfill to `''` automatically.
5. Super_admins can update recovery_email for any admin via the existing per-row modal pattern.

## Non-goals

- **Test-recovery-mailbox-per-admin** — admins remain hard to test without triggering an actual reset; super_admin would have to wait for an actual forgot-password event. Out of scope.
- **Self-service "update my own recovery_email"** — only `super_admin` can edit other admins' recovery emails. Admins who want to change their own personal email ask a super_admin. Out of scope.
- **Per-role recovery routing** — no `hr_admin → hr@`, `content_admin → content@` mapping. Each admin row carries its own address independently.
- **Rate-limiting / per-admin enumeration protection** — already documented as out-of-scope in v2.3.0 forgot-password spec.
- **Audit log** of which super_admin updated which admin's recovery email when. Out of scope.

## Decisions (confirmed)

| Question | Decision |
|---|---|
| Recipient when both per-admin and global are set | Per-admin wins |
| Recipient when per-admin is empty | Fall back to global setting |
| Required on new admin rows | Yes (app-level enforcement, NOT NULL DEFAULT `''` at the DB) |
| UI capture | Add modal (existing) + a per-row modal mirroring the existing per-field pattern |
| Strip whitespace on write | Yes (`set_recovery_email` rejects empty after `.strip()`) |
| Case-sensitivity | Store as typed. Not normalized. |
| Unique constraint | None — multiple admins may share an email (a small shared inbox is a legitimate destination). |
| `AdminUser` dataclass carries `recovery_email`? | No. Auth-shape stays minimal; the full row carries the column. `find_by_id` / `find_by_username` already include it. |

## Architecture

### 1. Schema migration

```sql
-- New column on `admins`. Migration runs in init_db after
-- `_migrate_admins_role` and `_migrate_admins_must_change`.
ALTER TABLE admins ADD COLUMN recovery_email TEXT NOT NULL DEFAULT '';
```

For fresh installs the column appears in the CREATE TABLE block:

```sql
CREATE TABLE IF NOT EXISTS admins (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  username             TEXT NOT NULL UNIQUE,
  password_hash        TEXT NOT NULL,
  role                 TEXT NOT NULL DEFAULT 'super_admin',
  recovery_email       TEXT NOT NULL DEFAULT '',
  created_at           TEXT NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 0
);
```

Migration is idempotent via `PRAGMA table_info` lookup; the DEFAULT backfills legacy rows to `''` automatically. The `''` value is the load-bearing sentinel: `''` means "no personal destination, fall back to global setting".

### 2. Service changes (`broadcaster/services/admin.py`)

- Extend `find_by_id` and `find_by_username` SELECT lists to include `recovery_email`.
- Extend `list_admins` SELECT list to include `recovery_email`.
- Extend `create_admin` signature: `create_admin(*, username, password, role, recovery_email="")`. The route layer validates `recovery_email` BEFORE calling; service persists as-is (after `.strip()`).
- New `set_recovery_email(admin_id, recovery_email)` parallel to `set_role` / `change_password`:
  - Reject empty (with `.strip()` applied) via `ValueError("recovery_email_required")` — but in practice the route's `validate_email` catches this first.
  - Reject missing-row via `ValueError("admin {id} not found")`.
- New helper `resolve_recovery_email(admin_row) -> str | None`:
  ```python
  def resolve_recovery_email(admin_row) -> str | None:
      per_admin = (admin_row["recovery_email"] or "").strip()
      if per_admin:
          return per_admin
      from broadcaster.services import settings as settings_svc
      global_email = (settings_svc.get("password_recovery_email") or "").strip()
      return global_email or None
  ```
- `bootstrap_admin()` needs **no change** — the column DEFAULT populates legacy inserts.

### 3. Validator change (`broadcaster/services/users.py`)

Promote `_validate_email` (private) to module-public `validate_email(value, *, required=True)`. Add `required=False` to existing callers; new admin route calls `validate_email(value, required=True)`.

```python
def validate_email(value: Optional[str], *, required: bool = True) -> Optional[str]:
    empty = (value is None or value == ""
             or (isinstance(value, str) and not value.strip()))
    if empty:
        if required:
            raise HTTPException(status_code=400, detail="invalid_email")
        return None
    if not EMAIL_RE.match(value):
        raise HTTPException(status_code=400, detail="invalid_email")
    return value
```

This is the single source of truth for email format checks; subscribers and admins share it.

### 4. Route changes (`broadcaster/routes/admins.py`)

- Extend `POST /api/admins` to require `recovery_email` in the payload, validate via `validate_email(recovery_email, required=True)`, pass to `admin_svc.create_admin(...)`.
- New `POST /api/admins/{admin_id}/recovery-email` (super_admin-only; router dependency already enforces):
  ```python
  @router.post("/{admin_id}/recovery-email")
  def set_recovery_email(admin_id: int, payload: dict = Body(...)):
      new_email = payload.get("recovery_email") or ""
      validated = validate_email(new_email, required=True)
      try:
          admin_svc.set_recovery_email(admin_id, validated)
      except ValueError as exc:
          raise HTTPException(status_code=404, detail=str(exc))
      row = admin_svc.find_by_id(admin_id)
      return dict(row)
  ```

Path style: kebab-case `recovery-email` mirrors the existing `role` / `password` siblings.

### 5. Forgot-password service (`broadcaster/services/password_reset.py`)

Replace the global-only lookup with `resolve_recovery_email`:

```python
recipient = admin_svc.resolve_recovery_email(row)
if not recipient:
    return (False, "recovery_mailbox_not_configured")
```

The email body gains a `Routed to: {recipient}` line so the operator / admin can see which address received the temp password (and whether it was the per-admin address or the global fallback).

### 6. UI changes (`templates/admin/admins.html` + `/static/js/admins.js`)

- Add modal `templates/admin/admins.html`:
  - New `recovery_email` field between Role and the action bar (with `<input type="email" required>` — browser-native format check on top of server-side validation).
- Add modal `templates/admin/admins.html`:
  - New `#recovery-email-modal` mirroring the `#password-modal` pattern; pre-populates from the row's `data-admin-recovery-email` attribute.
- Add row button "Recovery email" in actions cell (between "Change password" and "Delete").
- New column "Recovery email" in the All-admins table.
- `colspan="4"` → `colspan="5"` on the empty-state row.
- `/static/js/admins.js`:
  - New `openRecoveryEmailModal(adminId, username, currentEmail)` and `submitRecoveryEmail(ev)` mirroring `openPasswordModal` / `submitPassword`.
  - Update `rowHtml(a)` to render the new column with `escapeAttr(a.recovery_email || '')` and a `— (fallback)` placeholder when empty.

### 7. Settings page (`templates/admin/settings.html`)

Update the Password recovery card copy to make clear this address is the **fallback** used when admin rows have no email of their own. The card title changes from "Recovery mailbox" → "Fallback recovery mailbox" so operators who manage the global setting understand it's no longer the primary destination.

The `Test recovery mailbox` button and `testRecovery()` JS stay — they ping the same global address.

### 8. Tests

- Existing tests in `tests/test_password_reset.py` continue to pass without changes to the global fallback path (the bootstrap admin has `recovery_email=''` after the migration).
- New tests in `tests/test_password_reset.py` for the recipient-resolution branches:
  - `test_request_reset_uses_admin_recovery_email` — global empty, per-admin set → recipient is per-admin.
  - `test_request_reset_prefers_admin_over_global` — both set → recipient is per-admin.
  - `test_request_reset_falls_back_to_global_when_admin_empty` — per-admin empty, global set → recipient is global. (Largely covered by existing happy path; pin the body line.)
  - `test_request_reset_no_destinations_returns_config_error` — both empty → `recovery_mailbox_not_configured`.
- Update the existing `test_request_reset_happy_path_mints_password_and_sets_flag` to also assert `"Routed to: it-test@rollick.co.in"` appears in the body.
- New file `tests/test_admin_recovery_email.py` for the admin-management surface:
  - `test_create_admin_requires_recovery_email` — POST without → 400 invalid_email.
  - `test_create_admin_rejects_empty_string_recovery_email`
  - `test_create_admin_rejects_invalid_format_recovery_email` — covers several bad inputs.
  - `test_create_admin_accepts_valid_recovery_email`
  - `test_update_recovery_email_via_subpath`
  - `test_update_recovery_email_rejects_invalid_format`
  - `test_update_recovery_email_unknown_admin_returns_404`
  - `test_update_recovery_email_requires_field`
  - `test_list_admins_includes_recovery_email`
  - `test_bootstrap_admin_has_empty_recovery_email_post_migration` — pins the migration contract.
- Move the `recovery_settings` fixture from `tests/test_password_reset.py` into `tests/conftest.py` so `test_admin_recovery_email.py` can reuse it.

### 9. Files modified / created

**Modified:**
- `broadcaster/db.py` — schema line + `_migrate_admins_recovery_email` + call site + module-docstring update.
- `broadcaster/services/users.py` — promote `validate_email` with `required` flag.
- `broadcaster/services/admin.py` — extend SELECTs + `create_admin` + new `set_recovery_email` + `resolve_recovery_email`.
- `broadcaster/services/password_reset.py` — use `resolve_recovery_email`, add body line, drop unused `settings_svc` import.
- `broadcaster/routes/admins.py` — extend create + new `/recovery-email` endpoint.
- `broadcaster/templates/admin/admins.html` — Add modal field, new table column, new modal, action button, colspan update.
- `broadcaster/templates/admin/settings.html` — copy update (fallback semantics).
- `static/js/admins.js` — new modal handlers, `rowHtml` column, colspan update.
- `tests/conftest.py` — `recovery_settings` fixture moved here from `test_password_reset.py`.
- `tests/test_password_reset.py` — 4 new branch tests + body assertion on happy path + drop the local fixture.

**New:**
- `tests/test_admin_recovery_email.py`
- `docs/superpowers/specs/2026-07-14-admin-recovery-email-design.md` (this file).

### 10. Migration story

| Phase | What happens |
|---|---|
| App starts | `_migrate_admins_recovery_email` runs: `PRAGMA table_info(admins)` checks if `recovery_email` is present; if not, `ALTER TABLE admins ADD COLUMN recovery_email TEXT NOT NULL DEFAULT ''`. |
| Existing rows | `''` is materialized; the migration is a metadata-only operation in modern SQLite (no row rewrite). |
| `bootstrap_admin()` runs on a fresh DB | The INSERT doesn't mention `recovery_email`; the column DEFAULT fires and creates a `''`. The global `password_recovery_email` setting remains the only destination for this admin. |
| Operators backfilling | A super_admin opens `/admin/admins` and clicks "Recovery email" on each row. The per-admin modal posts `recovery_email` to `POST /api/admins/{id}/recovery-email`. After backfill, the admin row owns its own destination. |
| Future creates | The Add modal's `recovery_email` field is `required` and validated server-side; new admins are always created with a destination. |

### 11. Errors and edge cases

| Scenario | Behaviour |
|---|---|
| Admin row's `recovery_email` is non-empty | Temp password routes to it; body line is `Routed to: <that address>`. |
| Admin row's `recovery_email` is empty AND global is set | Temp password routes to the global setting; body line shows the global address. |
| Admin row's `recovery_email` is empty AND global is empty | Service returns `(False, "recovery_mailbox_not_configured")`; no email is sent. |
| `POST /api/admins` without `recovery_email` | 400 `invalid_email` (route layer, before service call). |
| `POST /api/admins` with bad-format `recovery_email` | 400 `invalid_email`. |
| `POST /api/admins/{id}/recovery-email` for a missing row | 404. |
| Bootstrap admin has no email set | Falls back to global. The operator should set it via the row's modal. |
| `recovery_email = " "` (whitespace-only) on write | `set_recovery_email` rejects as `recovery_email_required` after `.strip()`; route layer rejects as `invalid_email` since `validate_email(required=True)` checks `.strip()`. |

### 12. Open questions / out for v1

1. Should every admin on day one be backfilled with a real email automatically (e.g. pulled from `users.email` for an HR-admin row)? No — admins are separate from `users` table and have no `email` column there to draw from. Backend-only admins retain `''` until super_admin backfills.
2. Self-service update for the admin's own row — straightforward to add later (`POST /api/auth/recovery-email` mirroring `/api/auth/change-password`). Deferred.
3. Test-recovery-mailbox per admin — UI-affordance to send a "ping" to a specific admin's email without triggering a reset. Deferred.

### 13. Cross-references

- `2026-07-09-forgot-password-design.md` — this spec reuses the recipient-resolution pattern (`(False, "recovery_mailbox_not_configured")`, etc.) but supersedes its §11 "Per-admin recovery email" decision.
- `2026-07-01-admin-panel-ui-design.md` — the per-field modal pattern comes from this spec (`Change role`, `Change password`, `Delete`).
- `2026-07-01-rbac-refactor-design.md` — the `admins` table schema history; this spec adds the third column on top of `role` and `must_change_password`.
