# Forgot Password / Password Recovery — Design

**Status:** Approved (brainstorming complete)
**Date:** 2026-07-09
**Target version:** v2.3.0
**Scope:** Admin panel sign-in recovery only (subscribers / public viewers are not in scope).

## Context

Today an admin who forgets their password has no self-service recovery. The only path is for a super_admin to log in and use `/admin/admins → Change password` — which requires another super_admin to still have access. There is no way to recover the system when the only super_admin is locked out (e.g. forgotten password and no other super_admin exists).

This design adds a self-service "Forgot password?" flow. A single global recovery mailbox is configured on `/admin/settings`; when an admin requests a reset, a new temporary password is generated, hashed into `admins.password_hash`, and emailed to that mailbox. Whoever monitors the recovery mailbox relays the new password to the requesting admin out-of-band. The admin then logs in and is forced to set a permanent password on first sign-in.

The flow uses a **temporary password** (not a reset link) so the request doesn't require the admin to remember which inbox they originally registered with. The recovery mailbox being a single shared inbox is intentional — Rollick wants IT to gate access, not the user.

## Goals

1. Admin can self-serve a password reset from the login page.
2. Reset generates a strong random temporary password and emails it to a configured recovery mailbox.
3. The admin is forced to change the temporary password on first sign-in.
4. Configuration is one field on the existing `/admin/settings` page.
5. Existing SMTP service and existing `broadcaster/services/email.py` are reused — no new mail-send plumbing.

## Non-goals

- Reset-link tokens (would require per-admin email storage and a new table).
- Per-admin recovery email (decided as single global mailbox).
- MFA / second-factor.
- Account lockout after N failed reset requests.
- Subscriber-side password recovery (subscribers have no passwords).

## Decisions made during brainstorming

| Question | Decision |
|---|---|
| Where is the recovery email stored? | **Single global setting** in `settings` K/V |
| What does the recovery email contain? | **Temporary password** (not a reset link) |
| How does the admin get the temp password? | **IT relays it out-of-band** from the recovery mailbox |
| Failure-mode UX | **Strict / explicit errors** (return `no_such_admin`, etc. — no enumeration-masking) |

## Architecture

### 1. Configuration

A single new key in the existing `settings` K/V:

| key | default (seeded) | editable by |
|---|---|---|
| `password_recovery_email` | `anibandha.mukhopadhyay@rollick.co.in` | super_admin on `/admin/settings` |

The default is **seeded on first run** by `init_db` via `INSERT OR IGNORE`:

```python
conn.execute(
    "INSERT OR IGNORE INTO settings (key, value) VALUES "
    "('password_recovery_email', 'anibandha.mukhopadhyay@rollick.co.in')"
)
```

This is **not** in `SECRET_KEYS` (broadcaster/services/settings.py:66) — it's a routing address, not a credential.

A new `Test recovery mailbox` button on the settings page sends a one-line ping to the configured address, mirroring the existing `Test SMTP` button.

### 2. Schema migration

Add one column to `admins`:

```sql
ALTER TABLE admins ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
```

Migration is **idempotent**: try/except on duplicate-column error in `init_db` (broadcaster/db.py). All existing rows default to `0` ("don't force a change").

### 3. New service: `broadcaster/services/password_reset.py`

Single public function:

```python
def request_reset(username: str) -> tuple[bool, str]:
    """Returns (ok, detail). detail is one of:
      "sent"                            – success
      "no_such_admin"                   – username not in admins table
      "recovery_mailbox_not_configured" – settings.password_recovery_email is empty
      "smtp_not_configured"             – SMTP host or from-address not set
      "send_failed"                     – smtplib raised (still rotated pwd on DB;
                                          IT gets nothing — admin must retry)
    """
```

Flow inside `request_reset`:

1. `admin_svc.find_by_username(username)` — if `None`, return `(False, "no_such_admin")`.
2. Read `services.settings.get("password_recovery_email", "")` — if empty, return `(False, "recovery_mailbox_not_configured")`.
3. Read `get_settings()` — if `smtp_host` or `smtp_from` empty, return `(False, "smtp_not_configured")`.
4. `temp = security.generate_strong_password(length=14)`.
5. `admin_svc.change_password(admin_id=row["id"], new_password=temp)`.
6. `admin_svc.set_must_change_password(admin_id=row["id"], value=True)`.
7. `email.Email(...).send_message(to=recovery_addr, subject=..., body=...)`.
   - Subject: `[Rollick] Password reset requested for "{username}"`
   - Body (plain text):
     ```
     A password reset was requested for admin username "{username}" at {iso_timestamp}.

     New temporary password: {temp}

     Relay this password to the requesting admin out-of-band (phone, Teams, etc.).
     They will be required to set a permanent password on first sign-in.

     If you did not expect this request, no action is required.
     ```
   - On `smtplib.SMTPException`, **rotate the password back** by re-running `change_password` with a fresh `generate_strong_password` and clearing `must_change_password`. Return `(False, "send_failed")`.

`generate_strong_password` lives in `broadcaster/security.py`:

```python
import secrets, string
_ALPHABET = "".join(c for c in (string.ascii_letters + string.digits)
                   if c not in "0O1lI")
def generate_strong_password(length: int = 14) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
```

### 4. Forced password change on first login

Augment `load_current_admin` in `broadcaster/rbac.py`:

```python
def load_current_admin(request: Request) -> AdminUser:
    admin_id = request.session.get(SESSION_KEY)
    if admin_id is None:
        raise HTTPException(401, "not_authenticated")
    row = admin_svc.find_by_id(admin_id)
    if row is None:
        raise HTTPException(401, "not_authenticated")
    # Forced-change redirect: only let the change-password page and
    # logout through; everything else redirects.
    if row["must_change_password"]:
        path = request.url.path
        if path not in {
            "/admin/change-password",
            "/api/auth/change-password",
            "/api/auth/logout",
        } and not path.startswith("/static/"):
            # 303 redirect so the user lands on the change page.
            from fastapi.responses import RedirectResponse
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/admin/change-password"},
            )
    return AdminUser(id=row["id"], username=row["username"], role=row["role"])
```

(Exact redirect mechanism may need a small wrapper — see Open questions.)

### 5. New endpoints

In `broadcaster/routes/admin_auth.py`:

| Route | Method | Body / Params | Returns |
|---|---|---|---|
| `/api/auth/forgot-password` | POST | `{username: str}` | `{ok: bool, detail: str}` |
| `/api/auth/change-password` | POST | `{old_password: str, new_password: str, confirm: str}` | `{ok: True}` |
| `/admin/change-password` | GET | — | SSR HTML (no admin nav, just a centred card) |

In `app.py`:

| Route | Purpose |
|---|---|
| `/admin/forgot-password` | SSR page with username input |

`POST /api/auth/forgot-password` validates:
- `username` non-empty.
- Returns explicit detail codes per service contract.
- HTTP status: 200 on success, 400 on explicit-failure detail codes (`no_such_admin`, `recovery_mailbox_not_configured`, `smtp_not_configured`, `send_failed`), 500 only on programmer error.

### 6. UI changes

#### `broadcaster/templates/admin/login.html`

Add below the password field:

```html
<a href="/admin/forgot-password" class="muted" style="display:block; margin-top:12px; text-align:right;">
  Forgot password?
</a>
```

#### `broadcaster/templates/admin/forgot_password.html` (new)

- Mirrors the centred-card pattern of `login.html` (no admin nav).
- Single input: `Username`.
- Submit button posts to `/api/auth/forgot-password` via fetch.
- On success → green banner: *"A new temporary password has been emailed to the recovery mailbox. The user will be required to set a new password on first login."*
- On 400 → red banner with the explicit `detail` string (per strict-errors choice).
- Link back to `/admin/login`.

#### `broadcaster/templates/admin/change_password.html` (new)

- Old password / New password / Confirm.
- Same JS pattern as the existing `self-pw-modal` in `broadcaster/static/js/admins.js` (lift the validate-and-submit into a page-level form).
- On success: redirect to `/admin/`.
- Same confirm-match + ≥8-chars rules.

#### `broadcaster/templates/admin/settings.html`

Insert a new section after the SMTP controls:

```html
<div class="field">
  <span>Password recovery mailbox</span>
  <input name="password_recovery_email" type="email"
         value="{{ settings.password_recovery_email or '' }}">
</div>
<div class="form-actions">
  <button class="btn secondary small" data-test-recovery-mailbox>
    Test recovery mailbox
  </button>
</div>
```

The `Test` button POSTs to `/api/admin/settings/test-recovery-mailbox`, which sends a one-line "ping from Rollick" to the configured address and returns `{ok, detail}` (success or explicit error).

### 7. Data flow

```
[admin user]                           [recovery mailbox monitor]
   │                                              │
   │ GET /admin/forgot-password                   │
   ▼                                              │
[enter username, submit]                          │
   │                                              │
   ▼                                              │
POST /api/auth/forgot-password {username}        │
   │                                              │
   ▼                                              │
[bc.password_reset.request_reset]                 │
   ├─ 14-char temp password generated             │
   ├─ hashed into admins.password_hash             │
   ├─ admins.must_change_password = 1              │
   └─ SMTP -> recovery mailbox  ─────────────────► │  sees subject + body with temp pwd
                                                  │  phones / Teams-relays pwd to user
   ▼                                              │
[admin user receives pwd out-of-band]             │
   │                                              │
   ▼                                              │
POST /api/auth/login {username, temp_pwd}         │
   │                                              │
   ▼                                              │
session set; load_current_admin sees flag=1       │
   │                                              │
   ▼                                              │
303 -> /admin/change-password                     │
   │                                              │
   ▼                                              │
POST /api/auth/change-password                    │
   ├─ verify old == temp                          │
   ├─ new == confirm, len >= 8                    │
   ├─ hash & save new password                    │
   └─ must_change_password = 0                    │
   │                                              │
   ▼                                              │
303 -> /admin/                                    │
```

### 8. Error handling

| Scenario | Behaviour |
|---|---|
| Username not found | 400 `{"ok": false, "detail": "no_such_admin"}` |
| `password_recovery_email` not set | 400 `{"ok": false, "detail": "recovery_mailbox_not_configured"}` |
| SMTP not configured | 400 `{"ok": false, "detail": "smtp_not_configured"}` |
| SMTP send fails | rotate DB password back, return 400 `{"ok": false, "detail": "send_failed"}` |
| Login with wrong temp pwd | normal `invalid_credentials` 401 |
| Login with right temp pwd, old_password wrong on change | 400 `{"ok": false, "detail": "wrong_old_password"}` |
| Login with right temp pwd, new != confirm | client-side validation (also server-side check) |
| Login with right temp pwd, new < 8 chars | client-side + server-side check |
| Admin tries to hit any protected page while `must_change_password=1` | 303 → `/admin/change-password` |

### 9. Testing plan

New file `tests/test_password_reset.py`:

**Service unit tests:**
- `test_request_reset_unknown_user_returns_no_such_admin`
- `test_request_reset_no_recovery_mailbox_returns_recovery_mailbox_not_configured`
- `test_request_reset_no_smtp_returns_smtp_not_configured`
- `test_request_reset_happy_path_generates_password_hashes_sets_flag_sends_email`
- `test_request_reset_smtp_failure_rotates_password_clears_flag`
- `test_generate_strong_password_length_and_alphabet`

**Integration tests:**
- `test_get_forgot_password_page_renders`
- `test_post_forgot_password_happy_path_returns_200`
- `test_post_forgot_password_unknown_user_returns_400`
- `test_post_forgot_password_missing_config_returns_400`
- `test_login_with_temp_password_triggers_forced_change_redirect`
- `test_change_password_clears_flag_and_allows_normal_access`
- `test_change_password_rejects_wrong_old_password`
- `test_change_password_rejects_mismatched_confirm`
- `test_change_password_rejects_short_new_password`
- `test_settings_page_shows_password_recovery_email_field`
- `test_settings_saves_password_recovery_email`
- `test_test_recovery_mailbox_button_sends_ping`

**Regression:**
- All existing test files (`tests/test_auth.py`, `tests/test_rbac.py`, `tests/test_admins_page.py`, `tests/test_settings_hardening.py`, …) must remain green. The only schema delta is one additive nullable-by-default column.

### 10. Files modified / created

**Modified:**
- `broadcaster/db.py` — schema line for `must_change_password`, idempotent migration in `init_db`.
- `broadcaster/services/admin.py` — include `must_change_password` in `find_by_id` / `find_by_username` SELECTs; add `set_must_change_password(admin_id, value)`.
- `broadcaster/security.py` — add `generate_strong_password`.
- `broadcaster/rbac.py` — forced-change redirect in `load_current_admin`.
- `broadcaster/routes/admin_auth.py` — three new endpoints.
- `broadcaster/routes/admin_settings.py` — handle `password_recovery_email` in PUT; new `Test recovery mailbox` endpoint.
- `broadcaster/templates/admin/login.html` — Forgot-password link.
- `broadcaster/templates/admin/settings.html` — new field + Test button.
- `app.py` — `/admin/forgot-password` page route; `/admin/change-password` page route.
- `broadcaster/__init__.py` — bump `__version__` to `2.3.0`.

**New:**
- `broadcaster/services/password_reset.py`
- `broadcaster/templates/admin/forgot_password.html`
- `broadcaster/templates/admin/change_password.html`
- `tests/test_password_reset.py`

### 11. Out of scope (reaffirmed)

- Reset-link / token-table flow.
- Per-admin recovery email.
- Rate-limiting / enumeration protection (kept simple per strict-errors choice — internal tool, few admins).
- MFA / 2FA.
- Email templates with branded HTML — plain text only.
- Audit log of reset requests.

### 12. Open questions

1. **Redirect mechanism in FastAPI**: `HTTPException(303, headers={"Location": ...})` doesn't trigger a browser redirect from `load_current_admin` because the dependency raises before the response is built. The cleaner option is a small `Starlette` middleware or to check the flag at the top of every page-handler instead of in the global dep. Implementation will pick the cleaner of the two during plan-writing.

2. **Settings page editability for management**: management is currently allowed read-only access to `/admin/settings` (per RBAC refactor). **Default proposed: super_admin-only** for editing `password_recovery_email`, since changing the recovery mailbox is a security-sensitive action; management only views it. The plan-writing step will surface this for confirmation.

3. **Bootstrap safety**: if no admin exists yet (fresh install), the bootstrap flow creates one from env. The bootstrap admin must not be created with `must_change_password=1` — it defaults to `0`. No change needed to `bootstrap_admin`.