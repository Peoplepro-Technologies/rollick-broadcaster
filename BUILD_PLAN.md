# Rollick Broadcaster — Build Plan

**Product**: full-stack broadcast app. Admin schedules broadcasts (title + media + target group); each target subscriber gets a unique link; clicking the link plays the media and lets them leave an anonymous comment. Everything lives on one server.
**Scope**: v1 cut of a new app. Forked from `content_scheduler/` (per Agent A's recommendation) — reuses the backend integration glue, replaces the frontend.
**Source artifacts**: synthesized from 3 parallel investigations — `features.md` (target spec), Agent A's reuse analysis, Agent B's admin spec, Agent C's viewer spec.

---

## 1. Decisions (with the cross-spec tensions resolved)

| # | Question | Decision | Source |
|---|---|---|---|
| 1 | Repo strategy | **Fork to `BROADCASTER/`** — keep backend helpers, replace frontend, add viewer | Agent A |
| 2 | Frontend stack | FastAPI + **Jinja2 server-rendered templates** + `static/` for both admin and viewer. Drop the embedded-SPA-string pattern. Admin = multi-page (one page per tab); viewer = single SSR page. | Reconciles B + C |
| 3 | Auth | Admin: proper session cookie + bcrypt password hash (replace `admin/admin1234` literal). Viewer: **no auth** — token-in-URL is the credential. | A, B, C |
| 4 | Link model | **Per-(broadcast × subscriber) opaque token** — `secrets.token_urlsafe(24)` (~32 chars, 192 bits). Stored in `broadcast_links.token` UNIQUE. | B + C agree |
| 5 | Table naming | `broadcast_links`, `comments` (not `viewer_*` — the viewer is a route group, not a subsystem). | Reconciles B + C |
| 6 | Comment moderation | **Auto-publish** by default. Admin can `is_hidden=true` (soft hide, not delete). No queue in v1. | C |
| 7 | Media serving | Signed CDN URL (HMAC-SHA256, 10-min TTL). For v1 without a CDN, same-origin `Range`-supporting route. | C |
| 8 | Anti-spam | Adopt C's 12-layer list (token-only, expiry, honeypot, time-to-fill, per-IP rate, per-token cap, cooldown, profanity+link filter, length cap, CSRF, CSP, SameSite cookies). | C |
| 9 | Anonymity | Hash IPs with rotating server pepper (`SHA-256(ip + pepper)`). Pepper in env var, rotated quarterly. Never store raw IP, UA, or Referer. | C |
| 10 | Secrets | Env vars / `.env` (not in `settings` table). `settings` table holds only non-secret prefs. | A |
| 11 | Modernization | Replace `urllib` with `httpx`; pin WhatsApp API version in env; add `SMTP_SSL` support; per-tenant country-code. | A |
| 12 | Excel import | Keep full-replace for v1, but add **upsert mode** (default to upsert on phone) to prevent the "oops wiped my user list" footgun. | A |

---

## 2. Repo layout (fork to `BROADCASTER/`)

```
BROADCASTER/
├── features.md             ← already here
├── BUILD_PLAN.md           ← this file
├── KNOWLEDGE_TRANSFER.md   ← port from content_scheduler/, expanded
├── README.md
├── requirements.txt        ← NEW (was missing in content_scheduler/)
├── .env.example            ← NEW
├── .gitignore
├── Dockerfile              ← NEW
├── app.py                  ← main FastAPI app (router wiring only)
├── broadcaster.db          ← SQLite (gitignored, except for sample/seed)
├── uploads/                ← media storage (gitignored)
├── broadcaster/
│   ├── __init__.py
│   ├── db.py               ← init_db, get_db (from app.py:36-98)
│   ├── settings.py         ← pydantic Settings, env loading
│   ├── security.py         ← password hash, session cookie, CSRF
│   ├── auth.py             ← admin login/logout, dependency
│   ├── models/             ← pydantic schemas
│   │   ├── user.py
│   │   ├── group.py
│   │   ├── content.py
│   │   ├── broadcast.py
│   │   ├── link.py
│   │   ├── comment.py
│   │   └── settings.py
│   ├── routes/
│   │   ├── admin_auth.py
│   │   ├── admin_users.py
│   │   ├── admin_groups.py
│   │   ├── admin_content.py
│   │   ├── admin_broadcasts.py   ← includes link generation
│   │   ├── admin_analytics.py
│   │   ├── admin_comments.py     ← moderation
│   │   ├── admin_settings.py
│   │   └── viewer.py             ← public /v/{token} routes
│   ├── services/
│   │   ├── whatsapp.py      ← _send_whatsapp_batch, modernized to httpx
│   │   ├── email.py         ← _send_email_batch, SMTP_SSL support
│   │   ├── links.py         ← token mint, link gen per broadcast
│   │   ├── media.py         ← signed-URL HMAC, /v/{token}/media route
│   │   ├── ratelimit.py     ← per-IP-per-broadcast
│   │   ├── profanity.py     ← blocklist + leet normalize
│   │   └── analytics.py     ← view tracking, rollups
│   └── templates/
│       ├── base.html        ← shared layout (admin + viewer both extend)
│       ├── admin/
│       │   ├── login.html
│       │   ├── dashboard.html
│       │   ├── users.html
│       │   ├── groups.html
│       │   ├── content.html
│       │   ├── broadcasts_list.html
│       │   ├── broadcast_compose.html
│       │   ├── broadcast_detail.html
│       │   ├── history.html
│       │   ├── comments.html
│       │   └── settings.html
│       └── viewer/
│           ├── page.html
│           └── expired.html
├── static/
│   ├── css/
│   │   ├── tokens.css       ← design tokens (was :root in app.py:705-733)
│   │   ├── admin.css
│   │   └── viewer.css
│   └── js/
│       ├── admin.js         ← per-page small enhancements
│       └── viewer.js        ← comment form, cooldown
├── tests/
│   ├── conftest.py
│   ├── test_admin_users.py
│   ├── test_admin_groups.py
│   ├── test_admin_broadcasts.py
│   ├── test_viewer_resolve.py
│   ├── test_viewer_comments.py
│   └── test_antispam.py
└── scripts/
    ├── seed.py              ← demo data
    └── migrate_from_content_scheduler.py
```

---

## 3. Unified data model

SQLite, single `broadcaster.db`. `id` cols are `INTEGER PRIMARY KEY AUTOINCREMENT`. Times are ISO-8601 UTC strings.

```sql
-- ── Subscribers ────────────────────────────────────────────────
CREATE TABLE users (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  phone        TEXT NOT NULL,                  -- exactly 10 digits, UNIQUE
  email        TEXT,
  department   TEXT,                           -- drives auto-group: dept
  location     TEXT,                           -- drives auto-group: location
  is_active    INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_phone     ON users(phone);
CREATE INDEX        idx_users_dept      ON users(department) WHERE is_active=1;
CREATE INDEX        idx_users_location  ON users(location)   WHERE is_active=1;

-- ── Groups ─────────────────────────────────────────────────────
CREATE TABLE groups (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL,                   -- department|location|combo|manual
  criteria    TEXT,                            -- JSON; manual: {"user_ids":[…]"}
  is_auto     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_groups_auto ON groups(is_auto);

-- Many-to-many for manual groups only (auto groups compute on the fly)
CREATE TABLE group_memberships (
  group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id   INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_gm_user ON group_memberships(user_id);

-- ── Content library ────────────────────────────────────────────
CREATE TABLE content (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  content_type  TEXT NOT NULL,                 -- text|media
  caption       TEXT,
  content_data  TEXT,                          -- text body OR relative media path
  file_name     TEXT,
  file_size     INTEGER,
  mime_type     TEXT,
  created_at    TEXT NOT NULL
);

-- ── Broadcasts ─────────────────────────────────────────────────
CREATE TABLE broadcasts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  title            TEXT NOT NULL,
  category         TEXT NOT NULL DEFAULT 'General',
  message_text     TEXT,
  content_id       INTEGER REFERENCES content(id) ON DELETE SET NULL,
  delivery_channel TEXT NOT NULL DEFAULT 'whatsapp',  -- whatsapp|email|both
  scheduled_at     TEXT,                       -- NULL = draft
  sent_at          TEXT,                       -- NULL until send completes
  status           TEXT NOT NULL DEFAULT 'draft',     -- draft|queued|sending|sent|partial|failed
  whatsapp_status  TEXT,                       -- "sent:N,failed:M"
  email_status     TEXT,
  generate_links   INTEGER NOT NULL DEFAULT 1, -- on by default; off for plain email blasts
  created_by       TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX idx_broadcasts_status ON broadcasts(status);
CREATE INDEX idx_broadcasts_sched  ON broadcasts(scheduled_at);

-- Targets (groups OR explicit users)
CREATE TABLE broadcast_targets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
  group_id     INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  user_id      INTEGER REFERENCES users(id)  ON DELETE CASCADE,
  CHECK ((group_id IS NOT NULL) <> (user_id IS NOT NULL))
);
CREATE INDEX idx_bt_bcast ON broadcast_targets(broadcast_id);
CREATE INDEX idx_bt_user  ON broadcast_targets(user_id);
CREATE INDEX idx_bt_group ON broadcast_targets(group_id);

-- ── Unique per-subscriber links (core entity) ─────────────────
CREATE TABLE broadcast_links (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  broadcast_id  INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
  user_id       INTEGER NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
  token         TEXT NOT NULL,                 -- secrets.token_urlsafe(24)
  short_code    TEXT,                          -- optional vanity (NULL in v1)
  created_at    TEXT NOT NULL,
  expires_at    TEXT,                          -- NULL = never
  revoked_at    TEXT,                          -- soft revoke
  first_viewed_at TEXT,                        -- populated on first GET /v/{token}
  UNIQUE(broadcast_id, user_id),
  UNIQUE(token)
);
CREATE INDEX idx_bl_token ON broadcast_links(token);
CREATE INDEX idx_bl_bcast ON broadcast_links(broadcast_id);
CREATE INDEX idx_bl_user  ON broadcast_links(user_id);

-- ── Click/view tracking ────────────────────────────────────────
CREATE TABLE link_views (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id     INTEGER NOT NULL REFERENCES broadcast_links(id) ON DELETE CASCADE,
  viewed_at   TEXT NOT NULL,
  ip_hash     TEXT,                            -- SHA-256(ip + pepper); never raw
  ua_hash     TEXT,                            -- SHA-256(UA + pepper)
  referrer    TEXT
);
CREATE INDEX idx_lv_link ON link_views(link_id);
CREATE INDEX idx_lv_time ON link_views(viewed_at);

-- ── Anonymous comments (writer + reader) ──────────────────────
CREATE TABLE comments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id      INTEGER NOT NULL REFERENCES broadcast_links(id) ON DELETE CASCADE,
  broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id)      ON DELETE CASCADE,
  body         TEXT NOT NULL,                  -- already trimmed + sanitized
  author_hint  TEXT,                           -- optional "##1234" for display
  ip_hash      TEXT,                           -- for rate-limit dedupe
  status       TEXT NOT NULL DEFAULT 'visible',-- visible|hidden
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_comments_link   ON comments(link_id);
CREATE INDEX idx_comments_bcast  ON comments(broadcast_id, created_at DESC);
CREATE INDEX idx_comments_status ON comments(status, created_at DESC);

-- ── Settings (non-secret prefs only) ──────────────────────────
CREATE TABLE settings (
  key    TEXT PRIMARY KEY,
  value  TEXT
);
-- Keys: app_brand_name, app_timezone, base_public_url, link_token_ttl_days,
--       comment_max_per_ip_per_hour, comment_max_per_link_lifetime,
--       comment_cooldown_seconds, viewer_captcha_threshold
-- (Secrets live in env: SMTP_*, WHATSAPP_*)
```

**Naming reconciliation note**: Agent B called these `broadcast_links` and `comments`; Agent C called them `viewer_links` and `viewer_comments`. We adopt B's names — the viewer is a route group (`/v/...`), not a separate subsystem. The "viewer" terminology stays for templates/routes only.

---

## 4. API surface

### Admin (auth required except `/api/auth/login` and `/api/health`)

| Resource | Method + Path | Purpose |
|---|---|---|
| **Auth** | `POST /api/auth/login` | username/password → session cookie |
| | `POST /api/auth/logout` | kill session |
| | `GET  /api/auth/me` | current admin |
| **Stats** | `GET  /api/stats` | KPIs: users, groups, broadcasts (7d), views (7d) |
| **Users** | `GET /api/users` | list (filters: `active_only`, `q`, `dept`, `location`, `page`) |
| | `POST /api/users` | create one |
| | `PATCH /api/users/{uid}` | update |
| | `DELETE /api/users/{uid}` | delete |
| | `POST /api/users/bulk-delete` | multi (v2) |
| | `GET  /api/users/download` | `.xlsx` export |
| | `POST /api/users/upload-excel` | upsert-by-phone (v1 default) |
| | `GET  /api/users/preview` | filter preview |
| **Groups** | `GET/POST /api/groups`, `PATCH/DELETE /api/groups/{gid}` | CRUD |
| | `POST /api/groups/rebuild-auto` | force auto-group rebuild |
| | `GET/POST/DELETE /api/groups/{gid}/members` | manual membership |
| **Content** | `GET/POST/DELETE /api/content` | CRUD |
| | `POST /api/content/text` | text snippet |
| | `POST /api/content/media` | multipart upload |
| | `GET  /uploads/{path:path}` | admin-side file serve |
| **Broadcasts** | `GET  /api/broadcasts` | list (filters: status, channel, category, `with_links`, `q`, `page`) |
| | `POST /api/broadcasts` | create draft + auto-generate links if `generate_links=1` |
| | `GET  /api/broadcasts/{bid}` | detail + counts |
| | `PATCH /api/broadcasts/{bid}` | edit; regenerates links if targets changed |
| | `DELETE /api/broadcasts/{bid}` | cascades links/views/comments |
| | `POST /api/broadcasts/{bid}/schedule` | set `scheduled_at`, status → `queued` |
| | `POST /api/broadcasts/{bid}/send` | trigger immediate send |
| | `POST /api/broadcasts/{bid}/cancel` | withdraw |
| **Broadcast links** | `GET /api/broadcasts/{bid}/links` | per-link rollup |
| | `POST /api/broadcasts/{bid}/links/{lid}/revoke` | soft revoke |
| | `POST /api/broadcasts/{bid}/links/revoke-bulk` | filter-based |
| **Analytics** | `GET  /api/broadcasts/{bid}/analytics` | totals + per-link rollup + time-bucketed |
| | `GET  /api/broadcasts/{bid}/views.csv` | raw export |
| **Comments (mod)** | `GET /api/broadcasts/{bid}/comments` | list w/ status filter |
| | `PATCH /api/comments/{cid}` | hide/unhide |
| | `DELETE /api/comments/{cid}` | hard delete |
| | `POST /api/comments/{cid}/flag` | mark |
| **Settings** | `GET/POST /api/settings` | non-secret prefs |
| | `POST /api/settings/test-smtp` | verify SMTP |
| | `POST /api/settings/test-whatsapp` | verify WA |
| **Health** | `GET /api/health` | liveness + version |

### Public viewer (no auth)

| Method + Path | Purpose |
|---|---|
| `GET  /v/{token}` | SSR viewer page (resolves token, records view, renders media + comments + form) |
| `POST /v/{token}/comments` | persist anonymous comment |
| `GET  /v/{token}/media` | 302 → signed media URL (or same-origin `Range` stream in v1) |
| `POST /v/{token}/view` | idempotent view mark |

---

## 5. Pages

### Admin (one URL per page; sidebar nav across them)

| Page | Route | features.md § | v1? |
|---|---|---|---|
| Login | `/admin/login` | §2 | ✅ |
| Dashboard | `/admin/` | §4 | ✅ (KPI reshape: Users · Groups · Broadcasts · Views 7d) |
| Users | `/admin/users` | §5 | ✅ |
| Groups | `/admin/groups` | §6 | ✅ |
| Content | `/admin/content` | §7 | ✅ |
| Broadcasts (list) | `/admin/broadcasts` | replaces §8 | ✅ |
| Broadcast compose | `/admin/broadcasts/new` | new | ✅ |
| Broadcast detail | `/admin/broadcasts/{bid}` | new | ✅ |
| History | `/admin/broadcasts?status=sent` | §9 (folded into Broadcasts) | ✅ |
| Comments moderation | `/admin/comments` | new | ✅ |
| Settings | `/admin/settings` | §10 | ✅ |

**v1 cuts** (defer to v2): pagination, bulk row actions, soft delete / undo, keyboard shortcuts, dark mode, i18n, empty-state illustrations, real-time updates, drag-and-drop upload, a11y audit beyond the basics, in-app undo.

### Viewer (one page)

`GET /v/{token}` — server-rendered HTML with these sections:
1. **Header** — brand wordmark, broadcast title (H1)
2. **Media player** — `<video controls preload="metadata playsinline>` or `<img>` based on `mime_type`
3. **Meta strip** — category pill, "Posted {relative_time}", total comment count
4. **Comments list** — reverse-chronological, 20 per page, anonymous (no author, no avatar)
5. **Comment form** — `<textarea>` + honeypot `website` field + cooldown timer
6. **Footer** — "Powered by Rollick" + privacy link

Expired/revoked tokens → `GET /v/expired` template (or inline 410 with explanation).

---

## 6. Build order (phased)

Each phase ends with a runnable demo and at least one end-to-end test.

**Phase 0 — Scaffold** (½ day)
- Fork directory structure, `requirements.txt`, `Dockerfile`, `.env.example`, `.gitignore`
- Pydantic settings, env loading, db init, base Jinja layout, design-token CSS
- `pytest` + `httpx.AsyncClient` test client
- **Demo**: `uvicorn app:app` shows empty login page; `pytest` runs

**Phase 1 — Auth + Users + Groups + Content** (1–1.5 days)
- Admin auth: bcrypt + session cookie, replace hardcoded login
- Full Users CRUD + Excel import (upsert mode) + export
- Full Groups CRUD + auto-group rebuild
- Content CRUD + media upload + `/uploads/{path}` serve
- **Demo**: log in, manage users, upload content

**Phase 2 — Broadcast create + link generation** (1 day)
- `POST /api/broadcasts` resolves targets → mints one `broadcast_links` row per active user with `secrets.token_urlsafe(24)`
- PATCH regenerates links when targets change
- **Demo**: create a broadcast, see N links generated, list them in admin

**Phase 3 — Public viewer resolve** (½ day)
- `GET /v/{token}` → resolve to broadcast → render SSR page with media + meta
- Record `first_viewed_at` on first hit, `link_views` row
- 410 on expired/revoked
- **Demo**: paste a token URL into incognito, see the viewer

**Phase 4 — Send fan-out** (1 day)
- `POST /api/broadcasts/{bid}/send`: iterate links, render per-user message with `{{viewer_link}}` placeholder, push through `whatsapp.py` / `email.py` (modernized to `httpx`, `SMTP_SSL`)
- Update `whatsapp_status` / `email_status` counters
- **Demo**: send a test broadcast, receive WhatsApp/email with personal link, click it

**Phase 5 — Comments** (1 day)
- `POST /v/{token}/comments` with full validation chain
- 12-layer anti-spam (honeypot, time-to-fill, per-IP rate, per-token cap, cooldown, profanity, link filter, length cap, CSRF, CSP, SameSite, IP-hash dedupe)
- Comment form JS with cooldown timer
- **Demo**: post comments from a few "subscribers", see them appear in the viewer

**Phase 6 — Comments moderation** (½ day)
- Admin `/admin/comments` page: tabs (All / Hidden / Flagged), row-level hide/unhide
- **Demo**: hide a comment, see it disappear from public view but persist in DB

**Phase 7 — Analytics + per-link rollup** (½ day)
- `GET /api/broadcasts/{bid}/analytics`: total views, unique IPs (hashed), per-link status, time-bucketed
- Admin broadcast detail page shows the table
- **Demo**: send a broadcast, click 3 times from 2 IPs, see rollup

**Phase 8 — Settings + hardening** (½ day)
- SMTP/WhatsApp test buttons (port from content_scheduler, with `SMTP_SSL` and `httpx`)
- `app_brand_name`, `base_public_url`, `link_token_ttl_days` knobs
- CSP, SameSite=Strict, HSTS, rate-limit middleware
- **Demo**: configure SMTP, send test email, send a scheduled broadcast

**Total: ~6 days for v1** with one engineer.

---

## 7. Anti-spam (canonical, adopted from C)

| # | Layer | Rule |
|---|---|---|
| 1 | Token-only access | No enumeration of broadcasts |
| 2 | Token expiry | Default 30 days, configurable per broadcast |
| 3 | `first_viewed_at` flag | Mark on first GET, surfaced to admin |
| 4 | Honeypot | Hidden `website` field, CSS-hidden + `tabindex=-1` |
| 5 | Time-to-fill | `< 2s` or `> 2h` → reject |
| 6 | Per-IP rate | 5 comments / hour / broadcast; 20 / hour / IP global |
| 7 | Per-token cap | 3 comments / link / lifetime |
| 8 | Per-session cooldown | 1 comment / 30s per browser |
| 9 | Profanity + link filter | Blocklist + leet normalize, ≤1 `http(s)://` per comment |
| 10 | Body length | 2–500 chars, trimmed |
| 11 | CSRF | Per-session random token, double-submit cookie |
| 12 | CSP | `default-src 'self'; media-src https://cdn.rollick.app; img-src 'self' data:;` |

**CAPTCHA**: not in v1. Trigger if `viewer_comments` spam rate crosses 5% in 24h OR admin hides >10/day → enable hCaptcha for that broadcast only.

---

## 8. Open questions (consolidated from all 3 agents)

1. **Single-app vs two-app split** — is `/v/{token}` served by the same FastAPI process as admin (shared DB, shared secret boundary)? *Default: yes, single app, separate route group.* Tradeoff: simpler ops vs blast radius if a viewer bug ever leaks into admin.
2. **Per-user vs per-token media URLs** — link to `/v/{token}` (which loads media) or straight to media with token-as-sig? *Default: per-user viewer page (more flexible; comment UI lives there).* Straight-to-media is a fast-follow if perf demands.
3. **Link regeneration on target edit post-send** — refuse edits once `sent_at` is set, or regenerate for new targets? *Default: refuse.*
4. **Comment moderation queue** — auto-publish (proposed) vs. approval queue? *Default: auto-publish; admin can hide. No queue in v1.*
5. **WhatsApp template vs free-form** — WhatsApp Business API requires pre-approved templates for business-initiated messages. *Default: keep free-form (matches current behavior), but flag for production rollout — likely needs a `template_name` + `template_params` column later.*
6. **Email-of-record for comments** — anonymous-only (proposed) or support emailed magic link for edits? *Default: anonymous only. Decide before launch to avoid retro-migration.*
7. **"Already viewed" semantics** — does opening the page count, or does the `<video>` need to fire `play`? *Default: page open counts (`first_viewed_at` on GET).*
8. **Comment threading / reactions** — flat list (proposed), threaded, or also emoji reactions? *Default: flat list, no reactions. v2.*

---

## 9. What "done" looks like for v1

- `uvicorn app:app` serves admin at `/admin/` and viewer at `/v/{token}` on the same process
- Admin can: log in, manage users/groups/content, create a broadcast with a media file + target group, schedule or send immediately
- Each target user receives WhatsApp and/or email containing `https://<base_public_url>/v/{token>`
- Clicking the link in any browser/incognito shows the media player, meta, existing comments, and a comment form
- Posting a comment persists it; comments are auto-published, admin can hide from `/admin/comments`
- All 12 anti-spam layers active; CAPTCHA off
- All IPs hashed with rotating pepper; raw IP/UA/Referer never persisted or logged
- Settings page can send test SMTP/WhatsApp messages
- `pytest` passes; `broadcaster.db` starts empty; `scripts/seed.py` populates a working demo
- `Dockerfile` builds and runs the app
- `README.md` documents env vars, first-run setup, and the migration script from `content_scheduler/scheduler.db`

---

*Synthesized 2026-06-27 from three parallel investigations: Agent A (reuse analysis), Agent B (admin spec), Agent C (viewer spec). All cross-spec tensions resolved with named rationale. Source artifacts preserved for traceability.*
