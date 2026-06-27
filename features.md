# Frontend Features — Rollick Content Scheduler

**Audience:** PMs, designers, QA, new devs.
**Goal:** describe every visible feature in the SPA, what it does, and how to find it.

The frontend is a **single-page application served by FastAPI** — the entire HTML, CSS, and JavaScript lives inside `app.py` (≈ line 700+) as one Python string returned by `GET /`. There is no separate frontend build, no framework, and no router — tab switching is plain `display:none` / `display:block` toggled by a `switchTab()` function.

---

## 1. App shell

### Layout
Two-column layout, fixed sidebar on the left, scrollable main area on the right.

```
┌─────────┬──────────────────────────────────────────────┐
│ [brand] │  Page title · sub                            │
│         │                                              │
│  Nav    │  ┌──────────────────────────────────────┐    │
│  (7     │  │                                      │    │
│  items) │  │   Active tab content                 │    │
│         │  │                                      │    │
│         │  └──────────────────────────────────────┘    │
│ ─────── │                                              │
│ admin   │                                              │
│ Logout  │                                              │
└─────────┴──────────────────────────────────────────────┘
```

### Sidebar (left)
- **Brand**: pink gradient icon + "Rollick Content Scheduler" wordmark.
- **Nav**: 7 tabs, each a button with an emoji icon (see §3).
- **Footer**: shows logged-in user (`admin / admin@rollick.co.in` — hardcoded) and a Logout link.

### Main area
- **Page head**: large H1 page title + muted subtitle per tab (e.g. *"Users — Manage who receives your messages"*).
- **Tab content**: a stack of white `.card` panels.

### Color palette (CSS variables)
| Token | Value | Used for |
|---|---|---|
| `--primary` | `#ED0E6D` | Rollick brand pink — brand mark, primary buttons, active nav pill, KPI pink tile |
| `--primary-dark` | `#c40b5a` | Hover/active states of pink elements |
| `--surface` | `#ffffff` | Card backgrounds |
| `--bg` / `--bg-2` | `#f7f8fc` / `#eef0f7` | Page and surface-tinted backgrounds |
| `--text` / `--text-2` / `--muted` | `#0f172a` / `#475467` / `#98a2b3` | Body, secondary, hint text |
| `--success` / `--warning` / `--danger` / `--info` | `#16a34a` / `#f59e0b` / `#dc2626` / `#2563eb` | Status pills and toasts |
| `--shadow-sm/md/lg` | slate-tinted rgba | 3-tier elevation on cards, KPIs, modals |
| `--radius` / `--radius-lg` / `--radius-xl` | 10 / 16 / 20 | Standard rounded shape language |

### Typography
Inter (Google Fonts) — weights 400/500/600/700/800, with `-webkit-font-smoothing: antialiased`.

---

## 2. Login screen

A standalone view shown before authentication (the entire SPA is hidden until login succeeds).

- Centered card with a **4px gradient top bar** (pink → light pink) running edge-to-edge.
- **Radial-gradient page background** behind the card for a soft "modern SaaS" feel.
- Inputs: username + password.
- On submit: `POST /api/auth/login` with `Form(...)` fields. Hardcoded backend check for `admin` / `admin1234`.
- Success: stores the literal string `"cs_logged_in"` in browser `sessionStorage`, then reveals the app.
- Failure: red error text under the form.

> The auth token is a literal fixed string — no JWT, no expiry, no rotation.

---

## 3. The 7 tabs (sidebar nav)

Each button has an emoji icon, label, and `data-tab="..."` attribute. Clicking calls `switchTab(name)` which hides every `.tab-content` div and reveals the matching one.

| # | Icon | Label | Purpose |
|---|---|---|---|
| 1 | 📊 | Dashboard | KPI overview + quick actions |
| 2 | 👥 | Users | Manage recipients |
| 3 | 🏷️ | Groups | Auto + manual targeting groups |
| 4 | 📂 | Content | Reusable text + media library |
| 5 | 📨 | Schedule | Compose and queue a message |
| 6 | 📜 | History | Sent / pending / failed log |
| 7 | ⚙️ | Settings | SMTP + WhatsApp + test senders |

Active tab gets a pink pill background on the nav button.

---

## 4. Dashboard tab (📊)

**Header:** "Dashboard" / "Overview of users, groups, and scheduled messages"

### KPI tile row (3 tiles)
Pulled from `GET /api/stats`:

| Tile | Icon | Icon color | Number | Trend label |
|---|---|---|---|---|
| **Total Users** | 👥 | pink | `sUsers` | "Active in system" |
| **Groups** | 🏷️ | blue | `sGroups` | "Auto + manual" |
| **Pending** | ⏰ | amber | `sPending` | "Awaiting send" |

> The stats endpoint returns 5 fields (`total_users`, `active_users`, `groups`, `pending`, `content`) but the dashboard renders only 3 tiles — `active_users` and `content` are computed server-side and unused on screen.

### Quick actions card
A single card with three buttons that jump straight to other tabs:
- **Manage Users** → switches to Users
- **View Groups** → switches to Groups
- **+ Schedule Message** → switches to Schedule (green/success variant)

---

## 5. Users tab (👥)

**Header:** "Users" / "Manage who receives your messages"

### Toolbar
Right-aligned action buttons on the "All Users" card head:
- **+ Add User** (green) — opens the *Add User* modal (§10)
- **⤓ Excel** — downloads the current user list as `.xlsx` (`GET /api/users/download`)
- **↑ Upload** — opens a hidden `<input type="file" accept=".xlsx">`; on change calls `POST /api/users/upload-excel`

### Search
A full-width search input ("Search by name, phone, email...") filters the table client-side via `oninput="renderUsers()"`.

### Users table
Columns: **Name · Phone · Email · Department · Location · Status · (actions)**

- Status: shows a pill — green "Active" or muted "Inactive".
- Per-row action: delete button (with `confirm()` prompt).
- Phone numbers are validated to be exactly **10 digits** server-side; email format is checked. Duplicate phones return HTTP 409.

### Excel import
- `users.xlsx` lives at the project root and acts as the template/sample.
- `GET /api/users/preview` lets you validate before commit (endpoint exists; UI hookup is via the Upload button).
- Auto-groups are **rebuilt on every import** based on the dept/location fields in the new data.

---

## 6. Groups tab (🏷️)

**Header:** "Groups" / "Auto-generated from users + manual groups"

### Toolbar
- **+ Add Manual Group** (green) — opens the *Add Group* modal

### Groups table
Columns: **Name · Type · Source · Members · (actions)**

- **Auto groups** (rebuilt on user import):
  - One per distinct **department** (`Dept: <name>`, type=`department`)
  - One per distinct **location** (`Loc: <name>`, type=`location`)
  - One per **(department × location)** pair
- **Manual groups**: created via the modal; criteria stored as a JSON string in `groups.criteria`.
- Source column distinguishes "Auto" vs "Manual".
- Members column shows the count of users matching the criteria.
- Per-row action: delete (with confirm).

---

## 7. Content tab (📂)

**Header:** "Content" / "Reusable text + media library"

### Two sub-sections
- **+ Add Text** — opens a modal to save a reusable text snippet (`content_type = 'text'`)
- **+ Upload Media** — file input → `POST /api/content/media`, stored under `uploads/`, served back at `GET /uploads/{filename}`. Records `file_name`, `file_size`, `mime_type`.

### Content table
Lists saved items: text snippets show caption + body preview, media items show thumbnail / filename / size. Per-row delete.

These items are referenced by `scheduled_messages.content_id` so the same piece of content can be reused across many sends.

---

## 8. Schedule tab (📨) — the core composer

**Header:** "Schedule" / "Compose and send messages"

This is the highest-value screen in the app. Fields:

### Composer form
- **Title** — free text, internal label
- **Category** — dropdown from the `CATEGORIES` constant at the top of `app.py`: `General / Promotions / Alerts / Reminders / Announcements / Marketing`
- **Scheduled date & time** — `datetime-local` input
- **Delivery channel** — radio / segmented control: `WhatsApp` / `Email` / `Both`
- **Recipient groups** — **multi-select** picker (`msSelectAll` / `msSelectNone` helpers) — this is the v3.4 multi-select feature. Selection is sent as a comma-joined string of group ids in `target_group_id`.
- **Content** — either a free-text message body, or a pick from the Content library
- **Media** — optional attachment pulled from uploaded items

### Submit row
- **⏰ Schedule** — saves as `scheduled_messages` row with `sent = 0`, `scheduled_at` set
- **🚀 Send Now** — same payload but triggers delivery immediately via `POST /api/scheduled/{sid}/send`

### Important behavior note
> Scheduled messages **do not fire automatically**. They sit in the DB with `sent = 0` until a manual trigger (via the History tab's per-row "Send" button) hits the send endpoint. See `KNOWLEDGE_TRANSFER.md` §9 for why.

---

## 9. History tab (📜)

**Header:** "History" / "Past and pending messages"

### Filter bar
- Free-text search
- **Status filter** dropdown: `All / Pending / Sent / Failed`
- **Channel filter** dropdown: `All / WhatsApp / Email / Both`
- **Category filter** dropdown: `All / <each CATEGORY>`

### Messages table
Columns: **Title · Category · Channel · Recipients · Scheduled · Status · Sent at · (actions)**

- Status pill uses semantic colors: amber (Pending), green (Sent/Delivered), red (Failed), with separate `whatsapp_status` / `email_status` for the `Both` channel.
- Per-row action:
  - **Send** (for pending rows) → triggers `POST /api/scheduled/{sid}/send`
  - **Delete** (with confirm) → removes the row from history

---

## 10. Settings tab (⚙️)

**Header:** "Settings" / "Configure integrations"

Two clearly separated sections.

### SMTP section
Fields:
- SMTP host
- SMTP port (default 587)
- SMTP username
- SMTP password (stored, masked on read — see §11)
- SMTP "from" address

Buttons:
- **Save Settings** — upserts all fields via `POST /api/settings`
- **📧 Send Test Email** — calls `POST /api/settings/test-smtp` to verify config

### WhatsApp section
Fields:
- WhatsApp Phone Number ID
- WhatsApp Access Token (masked on read)
- WhatsApp App Secret (masked on read)

Buttons:
- **Save Settings**
- **📱 Send Test Message** — calls `POST /api/settings/test-whatsapp` to verify config

> The Settings tab is itself one of the headline v3.4 features ("…multi-select + settings" in the commit message) — it was promoted from an inline config into a first-class screen.

---

## 11. Modals

Two main modals, opened by `openAddUser()` and `openAddGroup()`:

### Add User modal
- Name (required)
- Phone (required, exactly 10 digits, unique)
- Email (optional, format-validated)
- Department (optional, free text — drives auto-grouping)
- Location (optional, free text — drives auto-grouping)
- Active checkbox (default on)
- **Save** / **Cancel**

### Add Group modal
- Group name (required)
- Type (text field — backend doesn't restrict)
- Criteria (free text or JSON-ish)
- **Save** / **Cancel**

Modal styling: dark scrim backdrop + centered card with `box-shadow: var(--shadow-lg)`, slide-in keyframe animation, click-outside-to-close.

---

## 12. Toasts

Transient feedback after most actions (login success/fail, save ok, delete ok, test send result). Slide-in animation, auto-dismiss after a couple of seconds.

Toast colors map to the semantic palette:
- Green → success
- Amber → warning
- Red → error
- Blue → info

---

## 13. Shared visual / interaction language

| Element | Pattern |
|---|---|
| **Buttons** | `.btn` (primary pink), `.btn.secondary` (outlined), `.btn.danger` (red), `.btn.success` (green), `.small` size variant |
| **Buttons hover** | `transform: translateY(-1px)` lift + colored glow `box-shadow` |
| **Cards** | white surface, 1px border, `--shadow-md`, 16px radius |
| **Inputs** | rounded, bordered, pink focus ring |
| **Tables** | `.table-wrap` scrollable container, sticky-feeling row separators |
| **Pills** | status indicators with semantic colors |
| **Forms** | `.field` rows with label + input + helper text |
| **Animation** | `@keyframes` for spinners, modal slide-in, toast slide-in |
| **Transitions** | `transition: all 0.15s` is the universal default |

---

## 14. What's "modern" about this UI (vs. a plain HTML form)

Concrete v3.4 design choices that back the "modern UI" claim in the latest commit message:

1. **CSS-variable design system** — semantic tokens (`--primary`, `--success`, `--shadow-md`, `--radius`) instead of hard-coded values everywhere.
2. **Inter typography** with antialiasing — gives the "polished SaaS" feel.
3. **Soft layered shadows** — 3-tier elevation on cards, KPI tiles, modals.
4. **Gradient brand accents** — pink → light pink on the brand mark and login top bar.
5. **Hover micro-interactions** — every button and nav item lifts on hover with a colored glow.
6. **KPI dashboard** with iconography and trend labels — not just numbers.
7. **Multi-select recipient picker** — a real tag-style selector on the Schedule tab (the "multi-select" half of v3.4).
8. **In-app "Send Test" buttons** for both channels — verify integrations without leaving the page.
9. **Toast notifications** — immediate visual confirmation of every action.
10. **Modal dialogs** for Add User / Add Group rather than inline forms.

---

## 15. Frontend gaps (what it does NOT have)

- **No dark mode** — light theme only, no `prefers-color-scheme` query, no toggle.
- **No drag-and-drop** — file upload uses a styled button + hidden `<input>`.
- **No real-time updates** — no WebSocket / SSE; refreshes are manual tab switches.
- **No undo / no soft delete** — deletes are immediate, no trash.
- **No pagination** — user/group/history tables render everything client-side; will degrade past a few thousand rows.
- **No keyboard shortcuts** — no ⌘K palette, no global hotkeys.
- **No accessibility audit visible** — buttons and inputs exist but `aria-*` roles, focus traps in modals, and skip-links are not evident.
- **No internationalization** — strings are inline English only.
- **No empty-state illustrations** — empty tables just say "No users yet" type text.
- **No bulk row actions** — there's no "select N users and delete" affordance.

---

*Generated 2026-06-27 from the same parallel exploration sweep that produced `KNOWLEDGE_TRANSFER.md`. Specifics (KPI tile count, sidebar footer text, exact form fields) were confirmed by targeted extraction from `app.py`.*
