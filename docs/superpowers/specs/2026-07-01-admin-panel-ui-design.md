# Admin Panel UI — 2026-07-01

> Add a `/admin/admins` page for `super_admin` to manage other admin
> accounts: roster view, role / password / delete mutations with
> confirmation modals and lockout-aware controls. The backend already
> exists from the 2026-07-01 RBAC refactor (`/api/admins/*`); this spec
> is the user-facing layer on top.

## Context

After the RBAC refactor, super_admin has the *capability* to create /
demote / delete / reset-password for other admins, but only via curl.
The `/api/admins/*` endpoints exist in `broadcaster/routes/admins.py`.
What's missing is a UI page so the super_admin can perform these
operations through the existing admin shell (topbar navigation,
template styling, modal affordances).

The user-facing app already has a well-established pattern for
table + modal CRUD pages (see `templates/admin/users.html` and
`static/js/users.js`). The new page mirrors that pattern verbatim.

## Goal & non-goals

**Goal:** super_admin can perform every operation on the
`/api/admins/*` API surface via a single new page (`/admin/admins`),
with lockout / self-delete controls defensively disabled in the UI
AND defensively enforced by the API.

**Non-goals (explicit):**

- Self password change for **non-super roles** — the self-account
  card reuses the existing super_admin-only endpoint. A separate
  follow-up adds `/api/auth/change-password` (any role, for self).
- Audit log of role / password / delete actions.
- Email invitation / magic-link onboarding.
- Bulk create from CSV.
- Password strength meter beyond a minimum length (see below).
- 2FA / TOTP — single-factor password only.
- Self-service forgot-password.
- Per-admin profile pictures / display names.

## Approach

**SSR + vanilla JS** (Approach A from brainstorming). The page is
server-rendered with the full initial state; mutations run via
`fetch()` against the existing `/api/admins` endpoints; a single
"reload the table" helper fetches the new state and swaps
`tbody.innerHTML`. This matches every other admin page.

No new framework dependency. No new backend code.

## Architecture

### New files

- `broadcaster/templates/admin/admins.html` — extends `base.html`,
  includes the standard nav, defines the page layout (page header
  + "Your account" card + "All admins" card + 4 modals) and the
  inline JS imports `static/js/admins.js`.
- `broadcaster/static/js/admins.js` — table rendering, modal
  open/close, fetch calls, lockout-flag computation.

### Modified files

- `app.py` — `@app.get("/admin/admins", response_class=HTMLResponse)`
  page handler. Uses the existing `_page_admin(request, "super_admin")`
  helper; on `("ok", admin)` renders the template with admins list
  fetched via `admin_svc.list_admins()`.
- `broadcaster/templates/admin/_nav.html` — new
  `<a href="/admin/admins">Admins</a>` between Comments and
  Settings, wrapped in `{% if current_admin.role == 'super_admin' %}`.
- `tests/test_rbac.py` — extend `PAGE_GATES` with the 4 cells for
  `/admin/admins`; add nav-render assertions (super_admin sees the
  link; management does not).
- `tests/test_admins_page.py` (new) — page-rendering tests,
  lockout-flag rendering, JS-driven reload smoke (if a headless
  harness is present; otherwise API-level coverage is sufficient).

### Backend

No changes. `/api/admins/*` from the RBAC refactor is the entire
backend. The page handler in `app.py` only adds the SSR endpoint
that returns HTML.

### Page layout

```
Page header — "Admins" + "Manage who can sign in…"

+─ Your account ───────────────────────────────────────────────+
|  Username: admin                                             |
|  Role:     super_admin  (badge)                              |
|  [Change my password]                                        |
+───────────────────────────────────────────────────────────────+

+─ All admins ─────────────────────────────────────────────────+
|  [+ Add admin]                                               |
|                                                              |
|  Username   Role             Created       Actions           |
|  ──────────────────────────────────────────────────          |
|  admin      [super_admin]   2026-07-01    [↻][🔑][🗑]      |
|  asha       [hr_admin]      today         [↻][🔑][🗑]      |
|  ravi       [content_admin] today         [↻][🔑][🗑]      |
+──────────────────────────────────────────────────────────────+
```

CSS reuses the existing `.card`, `.btn`, `.pill`, `.modal-backdrop`,
`.modal-card` primitives — no new design tokens.

### Modals

Four modals, each matching the existing `add-user-modal` /
`delete-user` style from `templates/admin/users.html`.

#### 3a. Add admin

| Field | Validation | Server error → UI |
|---|---|---|
| `username` | required, free-text | `409 username_taken` → inline error |
| `password` | required, ≥ 8 chars | n/a (validation is local) |
| `role` | required, one of 4 roles | `400 invalid_role` → inline error |

"Generate" button next to the password input fills a random
16-char string via `crypto.getRandomValues`.

#### 3b. Change role (per-row)

Pre-fills current role. On `409 LastSuperAdminError`: error reads
*"Cannot demote the last super_admin. Promote another admin to
super_admin first."* and on click a small `<a>` link scrolls the
window to a sibling super_admin row (if any other exists, else the
table top with "promote someone first" copy).

#### 3c. Change password (per-row)

Two fields (password + confirm), ≥ 8 chars each, both match. Submit
disabled until validated. On `404 admin_not_found`: error in modal.

#### 3d. Delete (per-row)

Confirm dialog: *"Delete admin '{username}'? They will lose access
immediately. This cannot be undone."* On `400 cannot_delete_self`:
"You can't delete your own account here." On `409`: "Cannot delete
the last super_admin."

#### 3e. Self-account password change

Reuses modal 3c's HTML but pre-fills the username readonly. POST
goes to `/api/admins/{self.id}/password` — works because
super_admin can change anyone's password, including their own. The
self-account card has its own modal instance separate from the
roster row modals.

### Lockout / self-disable

**Proactive disables** (computed client-side after each fetch):

1. **Self-delete guard** — for any row whose `username ===
   current_admin.username`: all three action buttons (`Change role`,
   `Change password`, `Delete`) are `disabled` with tooltip
   *"You can't manage your own account from the roster — use 'Your
   account' above."* (Even though the API would accept the role /
   password endpoints, we route them through the "Your account"
   card to make the affordance obvious.)
2. **Last-super_admin guard** — count rows with `role ===
   super_admin`; when exactly one such row exists and `current`
   isn't that row, the row's `Delete` button is disabled and the
   role `<select>` cannot demote that row's role (the same
   super_admin option is the only one not grayed-out).

**Defensive 409 handling** — every modal's error banner falls
back to the server's detail string. If the page is opened in two
tabs and the lockout state changes mid-flight, the server still
rejects; we never silently succeed.

## Data flow

| Step | What |
|---|---|
| `GET /admin/admins` | SSR: `_page_admin(request, "super_admin")` guard, `admin_svc.list_admins()` → table rows. Inject `<meta name="current-admin" content='{"id":1,"username":"admin","role":"super_admin"}'>` with the current admin serialized as JSON (single quotes around the attribute, JSON-escaped internally) so JS can read identity without an extra request. |
| `static/js/admins.js` on load | Read `current-admin` meta via `JSON.parse(document.querySelector(...).content)` → compute lockout-disabled flags → apply to row buttons |
| `POST /api/admins` | create — existing route |
| `POST /api/admins/{id}/role` | change role — existing route |
| `POST /api/admins/{id}/password` | password (incl. self) — existing route |
| `DELETE /api/admins/{id}` | delete — existing route |
| After every mutation | JS refetches `GET /api/admins`, swaps `tbody.innerHTML`, re-applies lockout flags |

## Testing

### Extend `tests/test_rbac.py`

Four new cells in `PAGE_GATES`:

- `/admin/admins` super_admin → 200
- `/admin/admins` hr_admin → 403
- `/admin/admins` content_admin → 403
- `/admin/admins` management → 403

Nav-render assertions:

- super_admin landing on `/admin/` sees `href="/admin/admins"` in
  the rendered nav.
- management landing on `/admin/users` does NOT see the link.

### New `tests/test_admins_page.py`

- Super_admin GET on `/admin/admins` returns 200.
- Body contains every existing admin's username.
- Body contains a `<meta name="current-admin">` whose JSON has
  the current admin's username + role.
- Body contains the "Your account" card showing current admin's
  username.
- Lockout rendering: with one super_admin seeded and management
  logged in attempting the page → 403 (covered above). With
  super_admin logged in and only one super_admin in the DB: that
  row's Delete button has the `disabled` attribute.
- After seeding a second super_admin via the API (as super_admin),
  re-fetching the page shows the lockout flag absent.
- Existing `/api/admins/*` lockout tests in `tests/test_rbac.py`
  continue to pass (no regression to the API layer).

### Manual smoke

1. `docker compose up -d && docker compose exec app pytest tests/test_admins_page.py`
2. Open `http://localhost:8123/admin/login`, log in as super_admin
   (default `admin` / your `ADMIN_PASSWORD`), visit `/admin/admins`.
3. Create an `hr_admin`, log out, log in as them, observe
   users / groups pages, logout, log back in as super_admin.
4. Demote, delete, change-password from the UI; verify the API and
   the UI both honor the last-super_admin lockout.

## Files added

- `broadcaster/templates/admin/admins.html`
- `broadcaster/static/js/admins.js`
- `tests/test_admins_page.py`

## Files modified

- `app.py` (one new page handler)
- `broadcaster/templates/admin/_nav.html` (one new gated nav item)
- `tests/test_rbac.py` (PAGE_GATES extension + nav assertions)

## Rollout

1. Pull the 10 RBAC commits (already on local main, not pushed).
2. Add this spec's implementation.
3. Manual smoke.
4. Push when ready.

## Risks

- **`current_admin` JSON in `<meta>`** — small XSS surface if the
  username contains `</script>`. Username is bounded to a known
  charset in the create form (no `<>`), and the API rejects
  non-printable input — but the JS uses `textContent` for the role
  badge and `data-*` attributes rather than `innerHTML` for safety.
- **Two-tab race** — covered by the server's 409 response. UI
  proactively disables but server is the source of truth.
- **Lockout state drift** — if another super_admin promotes / demotes
  in another tab while this one is open, the local cache is stale
  until the next mutation. Acceptable; the next mutation will fail
  with 409 anyway.
- **JS-disabled client** — the page still renders the roster SSR and
  reads from it; only mutations require JS. We'd add a "powered by
  JavaScript" warning if this becomes a concern.

## Open questions

None at design time. Roll forward.

## Status

**Implemented 2026-07-01.** Implementation plan: `docs/superpowers/plans/2026-07-01-admin-panel-ui.md`. Commits on `main`:

```
cb61877 feat(admin-panel): /admin/admins page handler + topbar nav link
02a0c04 test(admin-panel): API mirror tests for the JS-driven flow
b796e1b test(admin-panel): SSR + lockout rendering tests
```

13 admin-page tests pass (`tests/test_admins_page.py`): 6 SSR (page renders, list, current-admin meta JSON, self-account card, action buttons, add-admin button), 4 API-mirror (create / change-role / change-password / delete), 3 lockout (only-super_admin renders, two-super_admins renders, 409 surfaces).

Zero regressions: 129 tests pass across rbac + auth + viewer + admin-panel tests.

Manual smoke (do this in a browser): open `/admin/admins`, click through Add / Change role / Change password / Delete, verify Generate button, verify lockout behavior (only-super row has disabled Delete).
