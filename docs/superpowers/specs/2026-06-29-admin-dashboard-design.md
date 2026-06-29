# Admin Dashboard — Design Spec

**Date:** 2026-06-29
**Status:** Approved (pending user spec review)
**Scope:** Single iteration replacing the placeholder `/admin/` page.

## Problem

`/admin/` currently renders a single "Welcome" placeholder card with the comment *"Phase 1 dashboard placeholder. The real KPIs (Users · Groups · Broadcasts · Views 7d) wire in once Groups and Broadcasts land in Phase 1c/2."*

Phase 1c/2 are now shipped (groups + broadcasts + links + analytics + comments are all live). The placeholder should be replaced with a real operational dashboard that:

1. Glances the operational state of the system (counts, queue depth).
2. Surfaces recent broadcasts and the comment moderation queue.
3. Shows a 14-day views trend so the admin can see if engagement is moving.
4. Provides fast paths to the most common tasks (new broadcast, upload users).

The rest of BROADCASTER is SSR-first with minimal JS — the dashboard should follow the same pattern.

## Goals & non-goals

**Goals**

- Replace the placeholder with a real `/admin/` home page.
- Surface 6 KPI tiles, 1 line chart, 2 recent-queues panels, and 4 quick-action buttons.
- Read-only: every panel drills down to an existing detail page.
- No AJAX endpoints, no auto-refresh, no inline mutations.
- SSR-first: a single DB round-trip per page load via one new service function.

**Non-goals (this iteration)**

- No filtering / date-range selection / drill-down into the chart.
- No per-channel breakdown (WhatsApp vs email) — would need a `broadcasts.delivery_channel` slice.
- No growth-over-time (would need historical KPI snapshots — out of scope).
- No inline moderation actions on the dashboard (read-only decision).
- No new "flagged" comment state — "to moderate" = total visible comments.

## Layout

```
┌──────────────────────────────────────────────────────────┐
│ [pink topbar — already exists, unchanged]                │
├──────────────────────────────────────────────────────────┤
│  Dashboard                                               │
│  Operational snapshot of your broadcasts.                │
├──────────────────────────────────────────────────────────┤
│ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐    │
│ │Users │ │Active│ │Broad-│ │Views │ │Comm- │ │To    │    │
│ │ 1,234│ │ 980  │ │casts │ │ 7d   │ │ents7d│ │moder.│    │
│ │      │ │      │ │  42  │ │ 3,210│ │  127 │ │ ⚠3   │    │
│ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘    │
├──────────────────────────────────────────────────────────┤
│ ┌────────────────────────────┐ ┌─────────────────────┐    │
│ │ Views — last 14 days       │ │ Recent broadcasts   │    │
│ │ (Chart.js line)            │ │ Title · status · V  │    │
│ │                            │ │ 5 rows              │    │
│ └────────────────────────────┘ └─────────────────────┘    │
│ ┌────────────────────────────┐ ┌─────────────────────┐    │
│ │ Pending comments           │ │ Quick links         │    │
│ │ • "great offer" — bcast    │ │ + New Broadcast     │    │
│ │ • "when delivery?"         │ │ + Upload Users      │    │
│ │ • "thanks!"                │ │ • Manage Groups     │    │
│ └────────────────────────────┘ │ • Content Library   │    │
│                                └─────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

**Responsive**

- ≥1100px: 6 KPIs in one row; 2×2 cards below.
- 600–1099px: 3 KPIs per row; cards stack to single column.
- <600px: 2 KPIs per row; cards stack.

## KPI tiles

Six tiles, each is an `<a>` linking to the relevant detail page:

| Tile | Value | Sub-label | Link |
|---|---|---|---|
| Users | `users_total` | `users_new_week` new this week | `/admin/users` |
| Active users | `users_active` | inactive count | `/admin/users` |
| Broadcasts | `broadcasts_total` | all-time | `/admin/broadcasts` |
| Views (7d) | `views_week` | across all broadcasts | `/admin/broadcasts` |
| Comments (7d) | `comments_week` (visible only) | visible | `/admin/comments` |
| To moderate | `pending_mod` (total visible comments) | "oldest first" or "all clear" | `/admin/comments` |

When `pending_mod > 0`, that tile gets the `.kpi-warn` modifier: `--danger-soft` background, `--danger` border + value color.

Numbers formatted with `"{:,}".format(n)` so 1234 reads as "1,234".

## Recent broadcasts panel

Five most-recent broadcasts, ordered by `COALESCE(sent_at, created_at) DESC`. Columns:

- **Title** — `<a href="/admin/broadcasts/{id}">{title}</a>` + category below in `--muted` small text.
- **Status** — reuses the existing `.pill .pill.{modifier}` classes from `admin.css` (e.g., `sent → .pill.success`, `draft → .pill.muted`, `queued → .pill.info`, `failed → .pill.danger`).
- **Views** — `view_count / link_count` (e.g., "47 / 200").

Footer link "View all →" to `/admin/broadcasts`.

Empty state: "No broadcasts yet."

## Views chart (last 14 days)

A single Chart.js line chart, `canvas#views-chart` at 120px height. The chart is initialized on the client with `overview.views_by_day` data embedded inline via `|tojson`. No AJAX fetch.

```js
const data = {{ overview.views_by_day | tojson }};  // 14 entries
new Chart(ctx, {
  type: 'line',
  data: {
    labels: data.map(d => d.date.slice(5)),        // "MM-DD"
    datasets: [{
      label: 'Views',
      data: data.map(d => d.views),
      borderColor: '#ED0E6D',
      backgroundColor: 'rgba(237, 14, 109, 0.12)',
      fill: true, tension: 0.35, pointRadius: 3,
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { display: false } },
      y: { beginAtZero: true, ticks: { precision: 0 } }
    }
  }
});
```

**Chart.js source:** `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js` — pinned major version, no SRI hash (CDN-shipped, integrity-hash changes per release).

**Fallback:** if the CDN fails (CSP block / offline), wrap `new Chart(...)` in try/catch; on failure, replace the `<canvas>` with a static `<table>` of `views_by_day`.

## Pending comments panel

Five oldest visible comments, ordered `created_at ASC` (so the queue clears from the top). Each row:

- Body text (truncated to 80 chars with `…`).
- Broadcast title + date in `--muted` small text.

Click → `/admin/comments` (full queue). The `?focus={id}` highlight-on-jump feature is **out of scope** for this iteration; the dashboard drill-down is a plain link to the moderation page. (Adding focus-highlight later is a single-route-handler + small JS change if desired.)

Empty state: "No comments to review."

Footer link "Open queue →" to `/admin/comments`.

## Quick links panel

Four buttons, stacked vertically:

- `+ New Broadcast` → `/admin/broadcasts/new` (primary — uses `.btn.primary`)
- `↑ Upload Users` → `/admin/users`
- `Manage Groups` → `/admin/groups`
- `Content Library` → `/admin/content`

## Data layer

One new module: `broadcaster/services/dashboard.py`.

```python
# services/dashboard.py
from datetime import datetime, timedelta, timezone
from broadcaster.db import get_db


def dashboard_overview() -> dict:
    """Aggregates for /admin/. All time-bucketed queries use UTC."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    with get_db() as conn:
        users_total = _scalar(conn,
            "SELECT COUNT(*) FROM users")
        users_active = _scalar(conn,
            "SELECT COUNT(*) FROM users WHERE is_active = 1")
        users_new_week = _scalar(conn,
            "SELECT COUNT(*) FROM users WHERE created_at >= ?",
            (seven_days_ago,))
        broadcasts_total = _scalar(conn,
            "SELECT COUNT(*) FROM broadcasts")
        views_week = _scalar(conn,
            "SELECT COUNT(*) FROM link_views WHERE viewed_at >= ?",
            (seven_days_ago,))
        comments_week = _scalar(conn,
            "SELECT COUNT(*) FROM comments "
            "WHERE created_at >= ? AND status = 'visible'",
            (seven_days_ago,))
        pending_mod = _scalar(conn,
            "SELECT COUNT(*) FROM comments WHERE status = 'visible'")

        views_by_day = _views_by_day(conn, fourteen_days_ago)

        recent_broadcasts = conn.execute("""
            SELECT b.id, b.title, b.category, b.status, b.sent_at,
                   b.created_at, b.delivery_channel,
                   (SELECT COUNT(*) FROM broadcast_links bl
                    WHERE bl.broadcast_id = b.id) AS link_count,
                   (SELECT COUNT(*) FROM link_views lv
                    JOIN broadcast_links bl ON bl.id = lv.link_id
                    WHERE bl.broadcast_id = b.id) AS view_count
            FROM broadcasts b
            ORDER BY COALESCE(b.sent_at, b.created_at) DESC
            LIMIT 5
        """).fetchall()

        pending_comments = conn.execute("""
            SELECT c.id, c.body, c.author_hint, c.created_at,
                   b.title AS broadcast_title
            FROM comments c
            JOIN broadcasts b ON b.id = c.broadcast_id
            WHERE c.status = 'visible'
            ORDER BY c.created_at ASC
            LIMIT 5
        """).fetchall()

    return {
        "kpis": {
            "users_total": users_total,
            "users_active": users_active,
            "users_new_week": users_new_week,
            "broadcasts_total": broadcasts_total,
            "views_week": views_week,
            "comments_week": comments_week,
            "pending_mod": pending_mod,
        },
        "views_by_day": views_by_day,
        "recent_broadcasts": [dict(r) for r in recent_broadcasts],
        "pending_comments": [dict(r) for r in pending_comments],
    }


def _scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def _views_by_day(conn, since_iso: str) -> list[dict]:
    """Returns 14 entries: [{"date": "2026-06-15", "views": 42}, ...].
    Days with no views are filled with 0, so the chart's x-axis is contiguous."""
    rows = conn.execute("""
        SELECT substr(viewed_at, 1, 10) AS day, COUNT(*) AS n
        FROM link_views
        WHERE viewed_at >= ?
        GROUP BY day
        ORDER BY day
    """, (since_iso,)).fetchall()
    by_day = {r["day"]: r["n"] for r in rows}

    out = []
    start = datetime.fromisoformat(since_iso).date()
    for i in range(14):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"date": d, "views": by_day.get(d, 0)})
    return out
```

**No caching.** The dashboard is hit ~once per page-load by one admin; the query is cheap (8 indexed counts + 2 small selects + 1 14-bucket rollup). Adding `lru_cache` would invite stale-data bugs.

**Indexes needed:**

- `users(created_at)` — **MISSING** from current schema. Add `CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);` to `broadcaster/db.py` (required for the `users_new_week` query).
- `link_views(viewed_at)` — **already present** as `idx_lv_time` (line 120). No change needed.
- `comments(created_at)` — **covered** by the composite `idx_comments_status(status, created_at DESC)` (line 135). The `comments_week` query (`WHERE created_at >= ? AND status = 'visible'`) will use this composite index. No change needed.
- `comments.status` — covered by the same composite. No partial index needed at this volume.

The single new index goes into `broadcaster/db.py`'s `_SCHEMA` (or whatever the schema-creating constant is) using `CREATE INDEX IF NOT EXISTS` so it's safe to re-run on existing DBs.

## Route change

One handler in `app.py` (the existing `admin_dashboard` function at line 122):

```python
@app.get("/admin/", response_class=HTMLResponse)
async def admin_home(request: Request, response: Response):
    if not _is_admin(request):
        return _redirect("/admin/login")
    from broadcaster.services.dashboard import dashboard_overview
    overview = dashboard_overview()
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "app_name": settings.app_brand_name,
            "active_nav": "dashboard",
            "admin": {"username": "admin"},
            "overview": overview,
        },
    )
```

The existing placeholder dict (`{"username": "admin"}`) is replaced by the real service call.

## Frontend

**Template:** `broadcaster/templates/admin/dashboard.html` — fully replaces the current placeholder. Extends `base.html`, includes `admin/_nav.html`, renders the 4 sections described above.

**CSS:** add the `.kpi-grid`, `.kpi-tile`, `.kpi-warn`, `.dash-row`, `.dash-card-wide`, `.pending-list`, `.quick-links` blocks to `static/css/admin.css`. Bump `?v=7` → `?v=8` in `base.html`.

**JS:** Chart.js loaded via CDN tag inside the template's `{% block scripts %}`. The init script lives in the same block, uses `|tojson` for safe serialization of `overview.views_by_day`.

**CSP update:** the existing `add_security_headers` middleware in `app.py` (lines 60–72) sets `script-src 'self' 'unsafe-inline'`. Add `https://cdn.jsdelivr.net` to the `script-src` directive so Chart.js can be loaded from the CDN. Update `tests/test_settings_hardening.py::test_csp_header_present` to assert the new host is allowed.

## Error handling

- **SQLite error mid-query** — the transaction in `get_db()` already rolls back on exception. The route lets the global FastAPI exception handler return 500. No special handling needed because a broken DB is unrecoverable for the whole app, not just the dashboard.
- **Missing tables** (fresh DB before migrations) — `get_db()` applies `CREATE TABLE IF NOT EXISTS` at startup, so this shouldn't happen at runtime. If it ever does, `dashboard_overview()` returns an empty dict and the template renders all zeros + "No broadcasts yet" / "No comments to review". No try/except needed — failure mode is graceful.
- **Chart.js fails to load** — try/catch around `new Chart(...)`. On failure, swap `<canvas>` for a static HTML table listing `views_by_day`.
- **Network offline** — same as above; the page renders KPIs and tables from SSR; only the chart degrades.

## Testing

New file `tests/test_dashboard.py`:

```python
import pytest
from broadcaster.db import get_db
from broadcaster.services import users as users_svc
from broadcaster.services import broadcasts as bc_svc


async def _login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


async def test_dashboard_requires_auth(client):
    r = await client.get("/admin/", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers["location"]


async def test_dashboard_renders(authed_client):
    r = await authed_client.get("/admin/")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "Operational snapshot" in r.text


async def test_dashboard_empty_state(authed_client):
    """Fresh DB: all zeros, no crash, fallback copy visible."""
    r = await authed_client.get("/admin/")
    assert r.status_code == 200
    assert "No broadcasts yet" in r.text
    assert "No comments to review" in r.text
    assert "all clear" in r.text


async def test_dashboard_shows_kpis(authed_client):
    """Seed 3 users + 1 broadcast + 2 views + 1 comment, verify counts appear."""
    users = [users_svc.create_user(name=f"U{i}", phone=f"710000000{i}")
             for i in range(3)]
    b = bc_svc.create_broadcast(title="Hello", user_ids=[u["id"] for u in users])
    with get_db() as conn:
        links = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchall()
        for i, ln in enumerate(links[:2]):
            conn.execute(
                "INSERT INTO link_views (link_id, viewed_at, ip_hash, ua_hash) "
                "VALUES (?, datetime('now'), ?, ?)",
                (ln["id"], f"hash{i}", f"ua{i}"))
        first_link = links[0]["id"]
        conn.execute(
            "INSERT INTO comments (link_id, broadcast_id, body, ip_hash, status) "
            "VALUES (?, ?, 'hello', 'iphash', 'visible')",
            (first_link, b["id"]))
        conn.commit()

    r = await authed_client.get("/admin/")
    assert r.status_code == 200
    assert "3</span>" in r.text or ">3<" in r.text  # users_total
    assert "1</span>" in r.text or ">1<" in r.text  # broadcasts_total
    assert "2" in r.text                            # views_week (anywhere)


async def test_views_by_day_fills_zero_buckets():
    """14 entries returned even when some days have no views."""
    from broadcaster.services.dashboard import _views_by_day
    with get_db() as conn:
        out = _views_by_day(conn, "2020-01-01T00:00:00+00:00")
    assert len(out) == 14
    assert all(e["views"] == 0 for e in out)
    # dates are contiguous ISO days
    from datetime import date, timedelta
    expected_start = date(2020, 1, 1)
    for i, e in enumerate(out):
        assert e["date"] == (expected_start + timedelta(days=i)).isoformat()


async def test_views_by_day_counts_present_views():
    """Days with views reflect the COUNT(*) from link_views."""
    from broadcaster.services.dashboard import _views_by_day
    from broadcaster.db import get_db
    u = users_svc.create_user(name="X", phone="7100000099")
    b = bc_svc.create_broadcast(title="Y", user_ids=[u["id"]])
    with get_db() as conn:
        link_id = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchone()["id"]
        conn.execute(
            "INSERT INTO link_views (link_id, viewed_at, ip_hash, ua_hash) "
            "VALUES (?, '2026-06-29T10:00:00+00:00', 'h', 'u')",
            (link_id,))
        conn.commit()
        # Use a since-iso that includes only 2026-06-29
        out = _views_by_day(conn, "2026-06-29T00:00:00+00:00")
    # Day 0 should be 1, days 1..13 should be 0
    assert out[0]["views"] == 1
    assert all(e["views"] == 0 for e in out[1:])
```

**Existing test update** — extend `tests/test_settings_hardening.py::test_csp_header_present` to assert `cdn.jsdelivr.net` is in the script-src directive.

## Files changed

| File | Change |
|---|---|
| `broadcaster/services/dashboard.py` | **NEW** — `dashboard_overview()`, `_scalar()`, `_views_by_day()` |
| `broadcaster/templates/admin/dashboard.html` | **REPLACED** — placeholder → real layout |
| `app.py` | Updated handler `admin_dashboard` to call service; CSP `script-src` adds `cdn.jsdelivr.net` |
| `static/css/admin.css` | **APPENDED** — `.kpi-grid`, `.kpi-tile`, `.dash-row`, etc. |
| `templates/base.html` | Bump `admin.css?v=7` → `?v=8` |
| `broadcaster/db.py` | Add `CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);` |
| `tests/test_dashboard.py` | **NEW** — 6 tests above |
| `tests/test_settings_hardening.py` | Extend CSP test for jsdelivr |

## Out of scope (deferred)

- Inline moderation actions on the dashboard.
- Date-range selector on the views chart.
- Per-channel (WhatsApp / email) breakdowns.
- Auto-refresh / polling.
- Growth-over-time deltas on KPI tiles (would need historical snapshots).
- New "flagged" comment state for moderation queue.

## Open risks

- **CDN dependency** — if jsdelivr.net is blocked or down, the chart falls back to a static table; KPIs and lists still render. Acceptable.
- **CSP allowance for jsdelivr** — any future inline-script feature will need to also be permitted; keep CSP audit in mind.
- **Index cost** — adding 3 indexes on already-small tables is cheap; will revisit only if `link_views` grows to 100k+ rows.