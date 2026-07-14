# Rollick Broadcaster — Application Manual

**Audience:** Rollick staff who operate the broadcaster day-to-day (HR, content, management, super_admin). Covers everything an operator needs: setup, sign-in, password recovery, admin and subscriber management, broadcasts, and settings.

For developer / API references, see:
- [`README.md`](../README.md) — quick start + Docker
- [`BUILD_PLAN.md`](../BUILD_PLAN.md) — architecture and rationale
- [`docs/superpowers/specs/`](../superpowers/specs/) — per-feature design specs
- `/api/docs` — interactive OpenAPI reference (live server)

---

## 1. What this app does

An operator (HR, content, etc.) uses the admin panel to:
1. Manage **subscribers** — people who will receive broadcasts (their phone, name, dept, location).
2. Upload **content** — videos / images / PDFs that broadcasts reference.
3. Schedule **broadcasts** — pick a target group or individual subscribers, pick a channel (WhatsApp / email), and send. Each recipient gets a unique per-subscriber link.
4. Track **engagement** — view counts, comment counts, anonymous comments left on the public view page.

The viewer side is unauthenticated — anyone with the link can watch/listen and leave a comment.

---

## 2. Roles and access lanes

The broadcaster has four admin roles, set by `super_admin` on `/admin/admins`. Each role can reach a specific subset of pages:

| Role | Reaches | Typical use |
|---|---|---|
| **super_admin** | Everything, including other admin accounts and `/admin/settings` writes | One or two per deployment; the only role that can edit recovery emails, change roles, delete admins, edit SMTP/recovery settings |
| **hr_admin** | Users + Groups only | Manage the subscriber list and group definitions |
| **content_admin** | Content + Broadcasts + Comments | Upload media, schedule broadcasts, moderate comments |
| **management** | Read-only across the board | Dashboard, users (view), broadcast list, settings (read) |

Lanes are enforced by `broadcaster/rbac.py` — every protected route declares which roles can hit it, and the dependency turns a wrong-role request into a 403.

The **super_admin count is guarded**: an operation that would drop the count to zero (delete last super, demote to non-super, etc.) is rejected with `Cannot remove the last super_admin`. You can't lock yourself out.

---

## 3. First-time setup

1. Configure `.env` from `.env.example`. At minimum:
   - `ADMIN_USERNAME` (default `admin`)
   - `ADMIN_PASSWORD` (super-admin password on first boot)
   - `SESSION_SECRET` — 32+ random chars; rotates all sessions if changed
   - `IP_HASH_PEPPER` — pepper for hashing viewer IPs in `link_views` / `comments`
   - `MEDIA_SIGN_SECRET` — signs media URLs so they can't be hot-linked
2. Boot (Docker: `docker compose up -d`). On first startup the app:
   - Creates all SQLite tables (`broadcaster.db`)
   - Idempotently migrates any older schema
   - Seeds a default `password_recovery_email` setting (`anibandha.mukhopadhyay@rollick.co.in` — editable)
   - Bootstraps the first super_admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD`
3. Sign in at `/admin/login`. From there, change the bootstrap password immediately via the **Your account → Change my password** card.

If `ADMIN_PASSWORD` is empty or the bootstrap admin already exists, the bootstrap is a no-op.

---

## 4. Daily operations

### 4.1 Sign in

- `/admin/login` accepts username + password.
- On success: session cookie set; redirect to `/admin/` (dashboard).
- After **5 failed attempts in a session** the login form briefly locks. (Single-session, not IP-based.)
- "Forgot password?" link is on the login page — see § 5.

### 4.2 Admin management (`/admin/admins`)

This page lists every admin and the actions on their row. Only `super_admin` can reach it.

For each admin, the table shows: **Username / Role / Recovery email / Created / Actions**.

Actions available per row:

| Button | Effect |
|---|---|
| **Change role** | Pick a different role. Refuses if the demotion would drop super_admin count to 0. |
| **Change password** | Set a new password (must be ≥ 8 chars; confirm-match required). |
| **Recovery email** | Edit the per-admin inbox where Forgot-password emails go. Pre-fills with the current value. See § 5. |
| **Send recovery mail** | Generate a fresh temporary password right now and email it to that admin. Useful for onboarding or handing off credentials to a new admin. See § 5. |
| **Delete** | Hard-deletes the admin. Refuses if it would drop the super_admin count to 0. Cannot delete yourself. |

**Your account** card at the top of the same page lets you change your own password via the `/api/auth/change-password` flow (any role can use it for themselves).

### 4.3 Subscribers (`/admin/users`)

HR-maintained list of phone-keyed subscribers:

- **Add user:** name + phone (unique, 10-digit Indian mobile) + optional email, dept, location.
- **Edit:** any field. Setting `is_active=false` hides them from groups but doesn't delete history.
- **Delete:** hard delete. Cascades to memberships, links, comments.
- **Excel upload:** `/admin/users` → "Upload Excel" button. Destructive replace — see `features.md` §Excel import for the full contract (admin-protected, duplicate phones rejected per file, dept+location changes trigger group auto-rebuild).

### 4.4 Groups (`/admin/groups`)

Two kinds:
- **Manual** — created by an admin, members added by hand.
- **Auto** — created by the system from distinct `(department, location)` combinations. Member set updates as users change.

A recipient of a broadcast is either a *group* (all active members) or a single *user* (one-off). Both are selected when scheduling the broadcast.

### 4.5 Content library (`/admin/content`)

Upload media files. Each row has a content_type, optional caption, the file path, size, MIME type, and a created_at timestamp. Multiple broadcasts can reference the same content_id.

### 4.6 Broadcasts (`/admin/broadcasts`)

A broadcast = title + content + (one or more targets) + channel + (optional) schedule.

- **Channels:** `whatsapp` or `email`. WhatsApp dispatch goes through `AiSensy` (when its API key is configured) or falls back to the Meta Cloud API; `MockSender` is used in tests.
- **Scheduling:** schedule for a future timestamp, or click "Send now" for immediate dispatch. The status field progresses `draft → scheduled → sent`.
- **Per-recipient links:** each target subscriber gets a unique tokenized link. The link expires at `expires_at` (set when sending). Once revoked (`revoked_at`), the link stops working.
- **Analytics:** broadcasts page shows view counts per broadcast and comment counts.

### 4.7 Comments (`/admin/comments`)

Anonymous comments left on a view page. They are IP-hashed (`link_views.ip_hash` / `comments.ip_hash`) — the admin sees "1.2.3.x" prefix only. Comments can be moderated (hidden) by content_admin+.

---

## 5. Password recovery (v2.3.1)

The forgot-password flow gives a locked-out admin a way back in without needing another super_admin to intervene. v2.3.1 changed how it routes — read carefully, this is the headline feature of the release.

### 5.1 Two destinations, one rule

Every admin row carries a **`recovery_email`** column. When an admin triggers a reset (or a super_admin triggers one on their behalf), the temporary password is emailed to:

1. The **per-admin `recovery_email`** row (preferred), **or**
2. The **global `settings.password_recovery_email`** (fallback) — used when the row's `recovery_email` is empty.

Set `recovery_email` when you create an admin (Add modal → Recovery email field). Backfill later: `/admin/admins` → "Recovery email" button on the row.

### 5.2 User-initiated flow (`/admin/forgot-password`)

1. Admin clicks **Forgot password?** on `/admin/login`.
2. Admin enters their username, clicks Reset password.
3. Server validates: SMTP configured, recovery destination exists (per-admin row or global).
4. Server generates a 14-char strong random password (secrets.choice, ambiguous chars `0O1lI` stripped for relay readability).
5. Server hashes the new password into `admins.password_hash`, sets `must_change_password=1`.
6. Server emails the temporary password to the resolved recipient.
7. Admin checks their email (or asks IT to relay), signs in with the temp password.
8. Server sees `must_change_password=1`, redirects to `/admin/change-password`.
9. Admin sets a permanent password; flag clears; full access restored.

The temp password is sent **plaintext** to the recipient — the entire product design assumes an in-band password delivery with out-of-band relay by the recovery operator when needed. The fallback global setting seeds `anibandha.mukhopadhyay@rollick.co.in` on first boot but is editable at `/admin/settings` → "Fallback recovery mailbox".

### 5.3 Super-admin-initiated flow (`/admin/admins` → Send recovery mail)

Same machinery, but `super_admin` triggers it on behalf of an admin:

1. Click **Send recovery mail** on any row.
2. Modal shows the admin username and the resolved "Routed to" address (per-admin or fallback indicator).
3. Confirm.
4. Backend reuses `password_reset_svc.request_reset(username)` — same rules, same SMTP path, same rollback on send failure. The admin's existing password is rotated to a new temp password and the flag is set.

Use case: hand off credentials to a new admin who's never signed in, without making them visit the login page first.

### 5.4 Email body

This is the email the user (or operator) receives:

```
A password recovery request was received for the following account:

Username: <username>
Request Time: 14 July 2026, 05:45 UTC

A temporary password has been generated:

<temp-password>

Please use this temporary password to sign in. You will be required to
set a new permanent password upon your first sign-in.

For security, do not share this password with anyone.

If you did not request this password recovery, no action is required.

Regards,
Support Team
```

Date format example: `14 July 2026, 05:45 UTC` — day + full month + year + HH:MM UTC. Subject line: `[Rollick] Password recovery for <username>`.

### 5.5 Configuration knobs

| Knob | Where | Default | Notes |
|---|---|---|---|
| Per-admin `recovery_email` | `admins.recovery_email` | `''` | Set on create (required), or via the row's modal |
| Global fallback | `settings.password_recovery_email` | `anibandha.mukhopadhyay@rollick.co.in` | Edits at `/admin/settings` → "Fallback recovery mailbox" → "Save recovery mailbox". Empty disables the entire flow (per-admin rows still work). |
| SMTP host / from | env + `/admin/settings` | empty | Empty disables the entire flow. The settings page has a "Test SMTP" button to verify config without triggering a reset. |
| Test recovery mailbox | `/admin/settings` → "Test recovery mailbox" | — | Sends a one-line ping to the global fallback address so you can verify the routing address without resetting anything. |

### 5.6 Failures

| Failure | Visible behaviour |
|---|---|
| Username not in DB | Red banner: `no_such admin` (yes, strict-error — the spec is "explicit over masking" for this internal tool) |
| Both destinations empty | Red banner: `recovery_mailbox_not_configured` |
| SMTP not configured | Red banner: `smtp_not_configured` |
| SMTP send raised an exception | Password rotated forward to a fresh unknowable value, flag cleared, banner: `send_failed`. Login still works for the admin (with the new hash), but they've effectively been rotated to a new random password — they're not locked out behind the change-password screen. |
| Wrong old password on `/admin/change-password` | Red banner: `wrong_old_password` |

---

## 6. Settings (`/admin/settings`)

Card list of grouped config. Manage it carefully — settings drive dispatch behaviour.

### 6.1 App identity

- App name — displayed in browser title and emails.
- Base public URL — required for sign-in links, share links, recovery emails.

### 6.2 SMTP channel

`smtp_host`, `smtp_port`, `smtp_user`, `smtp_pass`, `smtp_from`. The `Test SMTP` button sends a tiny ping from `noreply@…` (or whatever the configured from is) to a target you specify, and reports delivery success/failure.

### 6.3 WhatsApp (Meta Cloud API)

`whatsapp_phone_id`, `whatsapp_access_token`, `whatsapp_app_secret`. Hidden for non-super_admin. The `Test WhatsApp` button sends a test message.

### 6.4 AiSensy (preferred WhatsApp provider)

> **Note (v2.3.1):** the AiSensy card is **hidden** on `/admin/settings` for this release. Backend configuration is preserved and broadcasts continue to route through AiSensy when its API key is set (env or DB). To re-enable the card: open `broadcaster/templates/admin/settings.html` and flip `{% if false %}` to `{% if true %}` around the AiSensy block (with the comment "AiSensy temporarily hidden — flip to true to re-enable").

When visible, it has the same shape as WhatsApp: API key, campaign name, base URL, `Check Credentials` button.

### 6.5 Fallback recovery mailbox

Edits `settings.password_recovery_email` — the global destination used when an admin row's `recovery_email` is empty. See § 5.

`Test recovery mailbox` button sends a one-line ping from the system to the configured address.

### 6.6 Per-admin recovery email

There's no global control here. Per-admin values live in `admins.recovery_email` and are edited row-by-row on `/admin/admins` → "Recovery email" button.

---

## 7. Reference

### 7.1 Environment variables (selected)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `broadcaster.db` | SQLite file path |
| `SESSION_SECRET` | random | Cookies are signed with this. Rotating it invalidates all sessions. |
| `IP_HASH_PEPPER` | empty (test rejects) | Salt for `link_views.ip_hash` and `comments.ip_hash` |
| `MEDIA_SIGN_SECRET` | empty (test rejects) | Signs media URLs |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `test-admin-pass` (dev only!) | Bootstrap super_admin on first boot |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | empty | Outgoing mail |
| `WHATSAPP_PHONE_ID` / `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_APP_SECRET` | empty | Meta Cloud API |
| `AISENSY_API_KEY` / `AISENSY_CAMPAIGN_NAME` / `AISENSY_BASE_URL` | empty | AiSensy provider |
| `COMMENT_COOLDOWN_SECONDS` / `COMMENT_MAX_PER_LINK_LIFETIME` | 30 / 5 | Comment anti-spam knobs |
| `PUBLIC_BASE_URL` | empty | App on a public URL; used in emails |

### 7.2 HTTP error codes (selected)

The frontend surfaces these strings verbatim in red banners; the backend uses HTTP status codes consistently.

| Detail code | Status | Where | Meaning |
|---|---|---|---|
| `not_authenticated` | 401 | any | No session cookie, or the admin row was deleted |
| `forbidden_for_role` | 403 | any | Role lane check failed |
| `invalid_credentials` | 401 | `/api/auth/login` | Wrong username / password |
| `too_many_attempts` | 429 | `/api/auth/login` | Per-session login lock |
| `invalid_phone` | 400 | `/api/users` | Phone not exactly 10 digits |
| `invalid_email` | 400 | `/api/users`, `/api/admins`, `/api/admins/{id}/recovery-email`, `/api/admins` | Bad email format (also: missing value when required) |
| `phone_taken` | 409 | `/api/users` | Unique-phone violation on insert |
| `username_taken` | 409 | `/api/admins` | Username collision |
| `username_password_role_required` | 400 | `/api/admins` | Required field missing (legacy code name, kept for backward compat) |
| `username_required` | 400 | `/api/auth/forgot-password` | Empty username |
| `no_such_admin` | 400 | `/api/auth/forgot-password`, `/api/admins/{id}/send-recovery-email` | Username/id not in `admins` table |
| `recovery_mailbox_not_configured` | 400 | forgot-password flow | No recovery destination at all |
| `smtp_not_configured` | 400 | forgot-password flow | SMTP host / from empty |
| `send_failed` | 400 | forgot-password flow | SMTP raised; password rotated forward, flag cleared |
| `wrong_old_password` | 400 | `/api/auth/change-password` | Old-password check failed |
| `password_too_short` | 400 | `/api/auth/change-password` | < 8 chars |
| `confirm_mismatch` | 400 | `/api/auth/change-password` | new ≠ confirm |
| `all_fields_required` | 400 | `/api/auth/change-password` | Empty old/new/confirm |
| `admin_not_found` | 404 | `/api/admins/{id}/...` | Target row is gone |
| `recovery_email_required` | 400 (raised as ValueError upstream) | `admin_svc.set_recovery_email` | Empty after `.strip()` |
| `last super_admin` | 409 | `/api/admins/{id}/role`, `/api/admins/{id}` | Refusing to drop last super_admin |
| `cannot_delete_self` | 400 | `/api/admins/{self_id}` | Self-delete refused |

### 7.3 Session security headers

The app sets these on every response (visible on `/admin/*` and the viewer):

- `Content-Security-Policy` — restricts scripts, styles, frame origins, etc.
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

If you front the broadcaster with a reverse proxy, **don't strip these** — the broadcaster relies on them.

---

## 8. Release notes — v2.3.1

**TL;DR** — per-admin recovery emails (with global fallback), super_admin "Send recovery mail" action, new password-recovery email body, AiSensy settings card hidden.

### Added

- **`admins.recovery_email TEXT NOT NULL DEFAULT ''`** column, with idempotent migration. Legacy rows backfill to `''` automatically. Bootstrap admin also starts at `''` and falls through to the global setting until someone sets it.
- **`POST /api/admins/{id}/recovery-email`** (super_admin) — set/edit another admin's recovery email with format validation.
- **`POST /api/admins/{id}/send-recovery-email`** (super_admin) — initiate forgot-password on behalf of an admin, returning the resolved recipient for UI feedback.
- **Forgot-password recipient resolution**: per-admin row wins, falls back to `settings.password_recovery_email` when the row is empty.
- **New email body template** for password recovery messages — clean separation of "request metadata / generated credential / instructions / security footer" with a "Regards, Support Team" signoff and a `Request Time: DD Month YYYY, HH:MM UTC` line.
- **Recovery email column on `/admin/admins`** + per-row "Recovery email" and "Send recovery mail" buttons.
- **Required `recovery_email` field on the Add admin modal** with format validation; the column must be non-empty on new rows.
- **`/admin/settings` Recovery card copy** updated to clarify it's the *fallback* destination.
- **Versioned static asset URLs**: `static/js/admins.js?v=15` now carries a version buster (matching the existing CSS convention), so browser caches invalidate when JS changes.

### Changed

- **Settings page fallback-recovery card**: copy now makes the fallback role explicit (per-admin first, this is what's used when no per-admin row exists).
- **`tests/test_password_reset.py`**: regex for `_extract_temp_pwd` updated to match the new label; the "Routed to:" body assertions were removed (line is gone).
- **`tests/test_static_assets.py`**: assertion updated to expect `?v=N` on the admins.js script tag.

### Hidden (preserved backend)

- **AiSensy card** on `/admin/settings` is wrapped in `{% if false %}…{% endif %}`. The backend, settings K/V, and the broadcast pipeline still use AiSensy. Flip `false` → `true` in `broadcaster/templates/admin/settings.html` to re-enable.

### Internal

- **`services/users.py`**: `_validate_email` promoted to module-public `validate_email(value, *, required=True)`. Subscriber callers pass `required=False`; admin callers pass `required=True`. Single source of truth for email format.
- **`services/admin.py`**: `find_by_id` / `find_by_username` / `list_admins` SELECTs include `recovery_email`. `create_admin` accepts `recovery_email`. New `set_recovery_email(admin_id, email)` and `resolve_recovery_email(admin_row) -> str | None` helpers.
- **`services/password_reset.py`**: uses `admin_svc.resolve_recovery_email(row)` instead of reading the global setting directly.

### Tests

- 5 new tests in `tests/test_admin_recovery_email.py` covering the Send-recovery-mail endpoint.
- 4 new tests in `tests/test_password_reset.py` for the recipient-resolution branches (per-admin wins, prefers over global, falls back, dual-empty).
- All touched suites green: `test_password_reset.py`, `test_admin_recovery_email.py`, `test_rbac.py`, `test_auth.py`, `test_admins_page.py`, `test_settings_hardening.py`, `test_static_assets.py`.
- 11 pre-existing failures in `test_users.py` (Excel import) and `test_dashboard.py` are NOT related to this release — they fail on `main` without these changes.

### Migration story

Existing deployments upgrade by:

1. Pull this version.
2. Boot. `init_db` runs `_migrate_admins_recovery_email`, which adds the column with DEFAULT `''`. Existing rows backfill automatically. Metadata-only on SQLite 3.35+.
3. Optionally visit `/admin/admins` and "Recovery email" each row whose bootstrap or pre-existing admin had no prior recovery email — without this, they continue to fall back to `settings.password_recovery_email`, which still works.
4. Sign in as usual; no other action required.

The default super_admin (bootstrap from `ADMIN_USERNAME`/`ADMIN_PASSWORD`) is left at `recovery_email=''` so existing deployments aren't surprised by a backfill that demands an email they don't have.

### Follow-ups not in v2.3.1

- **Test-per-admin recovery mailbox** — UI affordance to send a "ping" to a specific admin's email without triggering an actual reset.
- **Self-service "send my own recovery mail"** for any signed-in admin (`POST /api/auth/recovery-email` mirroring `/api/auth/change-password`).
- **Per-role email routing maps** (`hr_admin → hr@`, `content_admin → content@`, etc.).
- **Re-enable AiSensy card** on `/admin/settings` once the provider is back in active use.

---

## 9. Troubleshooting

### 9.1 "I forgot the only super_admin's password"

Use the bootstrap path:

1. Stop the app.
2. Set `ADMIN_USERNAME=admin` and `ADMIN_PASSWORD=new-strong-password` in `.env`.
3. Drop the old admin row directly: `sqlite3 broadcaster.db "DELETE FROM admins WHERE username='admin';"
4. Start the app — `bootstrap_admin()` recreates the row from env.

The new password takes effect on next login. **All previous comments/links/etc. are preserved** because none of them depend on the admins table.

### 9.2 "I see 'recovery_mailbox_not_configured' when an admin requests a reset"

Either the admin's row has `recovery_email=''` and the global setting is empty, OR the global setting hasn't been edited from its default. Fix one of:

- Edit the row on `/admin/admins` → Recovery email button.
- Edit `/admin/settings` → "Fallback recovery mailbox" → set to a real value.
- Verify "Save recovery mailbox" — the field is required at submit time.

### 9.3 "SMTP send raised — admin rotated, banner says send_failed"

`services/password_reset.py` rolls the password forward on send failure so the admin isn't stuck behind a change-password screen with a password nobody knows. The next time they reset, a fresh attempt is made. If this keeps failing:

- Click "Test SMTP" on `/admin/settings` to isolate the SMTP path from the recovery flow.
- Check `smtp_pass` (`SECRET_KEYS`, masked in UI; raw in the DB).
- Check Office365 / Gmail throttling — both will silently fail on too-frequent sends.

### 9.4 "Settings page doesn't show AiSensy anymore"

Expected in v2.3.1 — see § 6.4 and § 8. The card is one `{% if false %}` flip away from being visible again. The provider itself still works if its K/V is set.

### 9.5 "Comment count is 0 / view count is 0"

Browser likely has cookies disabled, or the link `expires_at` has passed, or `revoked_at` was set. Check the broadcast's status / view page manually.

### 9.6 "Excel upload rejected everyone / inserted 0"

`tests/test_excel_import_*` would tell you exactly which row failed and why. The destructive-replace pre-pass is aggressive — by design: a row missing from the file becomes a deletion. Make sure your file's row count matches what you expect to remain.

---

*End of v2.3.1 manual.*
