# Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/admin/` placeholder with a real operational dashboard — 6 KPI tiles, a 14-day views chart (Chart.js via CDN), recent broadcasts, pending comments queue, and quick-action links.

**Architecture:** SSR-first. One new service module (`broadcaster/services/dashboard.py`) aggregates all data in one DB round-trip. The existing `/admin/` route handler is updated to call the service and pass the result to a fully-rewritten `admin/dashboard.html`. Chart.js loads from jsdelivr CDN; the CSP middleware is updated to allow it. No new AJAX endpoints, no auto-refresh.

**Tech Stack:** FastAPI + Jinja2 + SQLite + Chart.js 4.4 (CDN). Existing pattern: SSR-first, vanilla JS only for client-side widgets.

**Spec:** `docs/superpowers/specs/2026-06-29-admin-dashboard-design.md`

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `broadcaster/db.py` | Modify (1 line) | Add `idx_users_created_at` index |
| `broadcaster/services/dashboard.py` | Create | `dashboard_overview()` + 2 helpers |
| `tests/test_dashboard.py` | Create | Service unit tests + route integration tests |
| `app.py` | Modify (1 handler + 1 CSP line) | Wire service + allow jsdelivr in CSP |
| `broadcaster/templates/admin/dashboard.html` | Replace | Full dashboard layout |
| `static/css/admin.css` | Append | `.kpi-grid`, `.kpi-tile`, `.dash-row`, etc. |
| `broadcaster/templates/base.html` | Modify (1 attr) | Bump `?v=7` → `?v=8` |
| `tests/test_settings_hardening.py` | Modify (1 line) | Extend CSP test to assert jsdelivr |

---

## Task 1: Add index on `users.created_at`

**Files:**
- Modify: `broadcaster/db.py:27-29`

- [ ] **Step 1: Add the index**

In `broadcaster/db.py`, immediately after the existing `idx_users_location` line (line 29), add:

```sql
CREATE INDEX        IF NOT EXISTS idx_users_created_at ON users(created_at);
```

The block (lines 27–29 + new line) should read:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone     ON users(phone);
CREATE INDEX        IF NOT EXISTS idx_users_dept      ON users(department) WHERE is_active=1;
CREATE INDEX        IF NOT EXISTS idx_users_location  ON users(location)   WHERE is_active=1;
CREATE INDEX        IF NOT EXISTS idx_users_created_at ON users(created_at);
```

- [ ] **Step 2: Verify the index is created**

Run:
```bash
python -c "
import os, tempfile, sqlite3
db = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
from broadcaster.db import init_db
init_db(db)
conn = sqlite3.connect(db)
rows = conn.execute(\"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_users_created_at'\").fetchall()
assert len(rows) == 1, rows
print('OK')
"
```

Expected: prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add broadcaster/db.py
git commit -m "db: add index on users.created_at for dashboard KPI"
```

---

## Task 2: Create `services/dashboard.py` — `_views_by_day` helper (TDD)

**Files:**
- Create: `broadcaster/services/dashboard.py`
- Create: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard.py`:

```python
"""Admin dashboard — service + route tests."""
from __future__ import annotations

from datetime import date, timedelta

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


# ── _views_by_day ────────────────────────────────────────────


def test_views_by_day_fills_zero_buckets():
    """14 entries returned even when some days have no views."""
    from broadcaster.services.dashboard import _views_by_day
    with get_db() as conn:
        out = _views_by_day(conn, "2020-01-01T00:00:00+00:00")
    assert len(out) == 14
    assert all(e["views"] == 0 for e in out)
    expected_start = date(2020, 1, 1)
    for i, e in enumerate(out):
        assert e["date"] == (expected_start + timedelta(days=i)).isoformat()


def test_views_by_day_counts_present_views():
    """Days with views reflect the COUNT(*) from link_views."""
    from broadcaster.services.dashboard import _views_by_day
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
        out = _views_by_day(conn, "2026-06-29T00:00:00+00:00")
    assert out[0]["views"] == 1
    assert all(e["views"] == 0 for e in out[1:])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_dashboard.py -v
```

Expected: ImportError on `broadcaster.services.dashboard` (module doesn't exist).

- [ ] **Step 3: Create the service skeleton with `_views_by_day`**

Create `broadcaster/services/dashboard.py`:

```python
"""Aggregations for the /admin/ dashboard.

One query batch per page load. No caching — the dashboard is hit ~once per
page-load by one admin and the queries are cheap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from broadcaster.db import get_db


def dashboard_overview() -> dict[str, Any]:
    """Return all data the dashboard template needs."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    with get_db() as conn:
        users_total = _scalar(conn, "SELECT COUNT(*) FROM users")
        users_active = _scalar(
            conn, "SELECT COUNT(*) FROM users WHERE is_active = 1")
        users_new_week = _scalar(
            conn, "SELECT COUNT(*) FROM users WHERE created_at >= ?",
            (seven_days_ago,))
        broadcasts_total = _scalar(
            conn, "SELECT COUNT(*) FROM broadcasts")
        views_week = _scalar(
            conn, "SELECT COUNT(*) FROM link_views WHERE viewed_at >= ?",
            (seven_days_ago,))
        comments_week = _scalar(
            conn,
            "SELECT COUNT(*) FROM comments "
            "WHERE created_at >= ? AND status = 'visible'",
            (seven_days_ago,))
        pending_mod = _scalar(
            conn, "SELECT COUNT(*) FROM comments WHERE status = 'visible'")

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


def _scalar(conn, sql: str, params: tuple = ()) -> int:
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

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_dashboard.py -v
```

Expected: 2 passed (`test_views_by_day_fills_zero_buckets`, `test_views_by_day_counts_present_views`).

- [ ] **Step 5: Commit**

```bash
git add broadcaster/services/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add dashboard_overview service + _views_by_day"
```

---

## Task 3: Add `dashboard_overview` tests (TDD)

**Files:**
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add tests for `dashboard_overview`**

Append to `tests/test_dashboard.py`:

```python
# ── dashboard_overview ──────────────────────────────────────


def test_dashboard_overview_empty_db():
    """Fresh DB returns zeros + empty lists, no crash."""
    from broadcaster.services.dashboard import dashboard_overview
    out = dashboard_overview()
    assert out["kpis"]["users_total"] == 0
    assert out["kpis"]["users_active"] == 0
    assert out["kpis"]["broadcasts_total"] == 0
    assert out["kpis"]["views_week"] == 0
    assert out["kpis"]["comments_week"] == 0
    assert out["kpis"]["pending_mod"] == 0
    assert out["recent_broadcasts"] == []
    assert out["pending_comments"] == []
    assert len(out["views_by_day"]) == 14


def test_dashboard_overview_kpis_reflect_seeded_data():
    """3 users, 1 broadcast, 2 views, 1 comment → counts match."""
    from broadcaster.services.dashboard import dashboard_overview
    users = [users_svc.create_user(name=f"U{i}", phone=f"71000000{i:02d}")
             for i in range(3)]
    b = bc_svc.create_broadcast(
        title="Hello", user_ids=[u["id"] for u in users])
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

    out = dashboard_overview()
    k = out["kpis"]
    assert k["users_total"] == 3
    assert k["users_active"] == 3
    assert k["broadcasts_total"] == 1
    assert k["views_week"] == 2
    assert k["comments_week"] == 1
    assert k["pending_mod"] == 1
    assert len(out["recent_broadcasts"]) == 1
    assert out["recent_broadcasts"][0]["title"] == "Hello"
    assert out["recent_broadcasts"][0]["link_count"] == 3
    assert out["recent_broadcasts"][0]["view_count"] == 2
    assert len(out["pending_comments"]) == 1
    assert out["pending_comments"][0]["body"] == "hello"


def test_dashboard_overview_excludes_hidden_comments_from_week_and_queue():
    """Hidden comments must NOT count toward comments_week or pending_mod."""
    from broadcaster.services.dashboard import dashboard_overview
    u = users_svc.create_user(name="U", phone="71000001")
    b = bc_svc.create_broadcast(title="T", user_ids=[u["id"]])
    with get_db() as conn:
        link_id = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchone()["id"]
        for body, status in [("vis", "visible"), ("hid", "hidden")]:
            conn.execute(
                "INSERT INTO comments (link_id, broadcast_id, body, ip_hash, status) "
                "VALUES (?, ?, ?, 'iphash', ?)",
                (link_id, b["id"], body, status))
        conn.commit()

    out = dashboard_overview()
    assert out["kpis"]["comments_week"] == 1
    assert out["kpis"]["pending_mod"] == 1
    assert len(out["pending_comments"]) == 1
    assert out["pending_comments"][0]["body"] == "vis"
```

- [ ] **Step 2: Run tests to verify they pass**

Run:
```bash
pytest tests/test_dashboard.py::test_dashboard_overview_empty_db \
       tests/test_dashboard.py::test_dashboard_overview_kpis_reflect_seeded_data \
       tests/test_dashboard.py::test_dashboard_overview_excludes_hidden_comments_from_week_and_queue \
       -v
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_dashboard.py
git commit -m "test(dashboard): add dashboard_overview tests"
```

---

## Task 4: Update `/admin/` route handler

**Files:**
- Modify: `app.py:122-130`

- [ ] **Step 1: Replace the handler body**

In `app.py`, replace lines 122–130 with:

```python
@app.get("/admin/", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services.dashboard import dashboard_overview
    overview = dashboard_overview()
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"app_name": get_settings().app_name, "active_nav": "dashboard",
         "admin": {"username": "admin"}, "overview": overview},
    )
```

- [ ] **Step 2: Verify the import already exists**

Run:
```bash
grep -n "from broadcaster.services.dashboard" app.py
```

Expected: one match (the new line we just added). No other imports needed since `RedirectResponse`, `Request`, `HTMLResponse`, `templates`, and `get_settings` are already in scope.

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat(dashboard): wire admin_dashboard to dashboard_overview service"
```

---

## Task 5: Allow jsdelivr in CSP

**Files:**
- Modify: `app.py:64-73` (the CSP string inside `add_security_headers`)

- [ ] **Step 1: Add jsdelivr to `script-src`**

In `app.py`, change line 70:

```python
        "script-src 'self' 'unsafe-inline'; "
```

to:

```python
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
```

Leave all other directives unchanged.

- [ ] **Step 2: Commit**

```bash
git add app.py
git commit -m "feat(dashboard): allow cdn.jsdelivr.net in CSP for Chart.js"
```

---

## Task 6: Extend CSP test to assert jsdelivr

**Files:**
- Modify: `tests/test_settings_hardening.py:84-91` (`test_csp_header_present`)

- [ ] **Step 1: Add jsdelivr assertion**

In `tests/test_settings_hardening.py`, replace `test_csp_header_present`:

```python
async def test_csp_header_present(client):
    r = await client.get("/api/health")
    csp = r.headers.get("content-security-policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "https://cdn.jsdelivr.net" in csp
```

- [ ] **Step 2: Run the test**

Run:
```bash
pytest tests/test_settings_hardening.py::test_csp_header_present -v
```

Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_settings_hardening.py
git commit -m "test(csp): assert cdn.jsdelivr.net allowed in script-src"
```

---

## Task 7: Replace `dashboard.html` with the real layout

**Files:**
- Modify: `broadcaster/templates/admin/dashboard.html` (full overwrite)

- [ ] **Step 1: Overwrite the template**

Replace the entire contents of `broadcaster/templates/admin/dashboard.html` with:

```html
{% extends "base.html" %}
{% block title %}Dashboard — {{ app_name }}{% endblock %}
{% block body_class %}app-shell{% endblock %}

{% block body %}
{% include "admin/_nav.html" %}

<main class="main">
  <div class="page-head">
    <div>
      <h1>Dashboard</h1>
      <p class="sub">Operational snapshot of your broadcasts.</p>
    </div>
  </div>

  {# ── KPI tiles ──────────────────────────────────────────── #}
  <div class="kpi-grid">
    {% set k = overview.kpis %}
    <a class="kpi-tile" href="/admin/users">
      <span class="kpi-label">Users</span>
      <span class="kpi-value">{{ "{:,}".format(k.users_total) }}</span>
      <span class="kpi-sub">{{ k.users_new_week }} new this week</span>
    </a>
    <a class="kpi-tile" href="/admin/users">
      <span class="kpi-label">Active users</span>
      <span class="kpi-value">{{ "{:,}".format(k.users_active) }}</span>
      <span class="kpi-sub">{{ k.users_total - k.users_active }} inactive</span>
    </a>
    <a class="kpi-tile" href="/admin/broadcasts">
      <span class="kpi-label">Broadcasts</span>
      <span class="kpi-value">{{ "{:,}".format(k.broadcasts_total) }}</span>
      <span class="kpi-sub">all-time</span>
    </a>
    <a class="kpi-tile" href="/admin/broadcasts">
      <span class="kpi-label">Views (7d)</span>
      <span class="kpi-value">{{ "{:,}".format(k.views_week) }}</span>
      <span class="kpi-sub">across all broadcasts</span>
    </a>
    <a class="kpi-tile" href="/admin/comments">
      <span class="kpi-label">Comments (7d)</span>
      <span class="kpi-value">{{ "{:,}".format(k.comments_week) }}</span>
      <span class="kpi-sub">visible</span>
    </a>
    <a class="kpi-tile {% if k.pending_mod %}kpi-warn{% endif %}" href="/admin/comments">
      <span class="kpi-label">To moderate</span>
      <span class="kpi-value">{{ "{:,}".format(k.pending_mod) }}</span>
      <span class="kpi-sub">{% if k.pending_mod %}oldest first{% else %}all clear{% endif %}</span>
    </a>
  </div>

  {# ── Chart + Recent broadcasts ──────────────────────────── #}
  <div class="dash-row">
    <div class="card dash-card-wide">
      <div class="card-head"><h2>Views — last 14 days</h2></div>
      <canvas id="views-chart" height="120"></canvas>
    </div>
    <div class="card">
      <div class="card-head">
        <h2>Recent broadcasts</h2>
        <a class="muted small" href="/admin/broadcasts">View all →</a>
      </div>
      <table class="data-table compact">
        <thead><tr><th>Title</th><th>Status</th><th>Views</th></tr></thead>
        <tbody>
        {% for b in overview.recent_broadcasts %}
          <tr>
            <td><a href="/admin/broadcasts/{{ b.id }}">{{ b.title }}</a>
              <div class="muted small">{{ b.category or '—' }}</div></td>
            <td><span class="pill {{ _status_pill_class(b.status) }}">{{ b.status }}</span></td>
            <td>{{ b.view_count }} / {{ b.link_count }}</td>
          </tr>
        {% else %}
          <tr><td colspan="3" class="muted">No broadcasts yet.</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  {# ── Pending comments + Quick links ─────────────────────── #}
  <div class="dash-row">
    <div class="card">
      <div class="card-head">
        <h2>Pending comments</h2>
        <a class="muted small" href="/admin/comments">Open queue →</a>
      </div>
      <ul class="pending-list">
      {% for c in overview.pending_comments %}
        <li>
          <a href="/admin/comments">{{ c.body[:80] }}{% if c.body|length > 80 %}…{% endif %}</a>
          <span class="muted small">— {{ c.broadcast_title }} · {{ c.created_at[:10] }}</span>
        </li>
      {% else %}
        <li class="muted">No comments to review.</li>
      {% endfor %}
      </ul>
    </div>
    <div class="card">
      <div class="card-head"><h2>Quick links</h2></div>
      <div class="quick-links">
        <a class="btn primary" href="/admin/broadcasts/new">+ New Broadcast</a>
        <a class="btn" href="/admin/users">↑ Upload Users</a>
        <a class="btn" href="/admin/groups">Manage Groups</a>
        <a class="btn" href="/admin/content">Content Library</a>
      </div>
    </div>
  </div>
</main>
{% endblock %}

{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
  (function () {
    const data = {{ overview.views_by_day | tojson }};
    const labels = data.map(d => d.date.slice(5));
    const values = data.map(d => d.views);
    const ctx = document.getElementById('views-chart');
    try {
      new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Views',
            data: values,
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
    } catch (e) {
      // Fallback: render a static table if Chart.js failed to load.
      const table = document.createElement('table');
      table.className = 'data-table compact';
      table.innerHTML = '<thead><tr><th>Date</th><th>Views</th></tr></thead><tbody>'
        + data.map(d => '<tr><td>' + d.date + '</td><td>' + d.views + '</td></tr>').join('')
        + '</tbody>';
      ctx.replaceWith(table);
    }
  })();
</script>
{% endblock %}
```

- [ ] **Step 2: Add `_status_pill_class` global helper**

The template uses `{{ _status_pill_class(b.status) }}` to map a broadcast status string to the existing `.pill` modifier classes (`.success`, `.muted`, `.info`, `.warning`, `.danger`). Open `app.py` and find the `templates = Jinja2Templates(...)` line (line 82). Immediately after, register the global:

```python
def _status_pill_class(status: str) -> str:
    """Map a broadcast status string to an admin.css .pill modifier class."""
    return {
        "sent": "success",
        "queued": "info",
        "scheduled": "info",
        "draft": "muted",
        "sending": "warning",
        "failed": "danger",
        "cancelled": "muted",
    }.get(status, "muted")

templates.env.globals["_status_pill_class"] = _status_pill_class
```

- [ ] **Step 3: Verify the template parses**

Run:
```bash
python -c "from broadcaster.templates_setup import get_env; env = get_env(); env.get_template('admin/dashboard.html')"
```

If the project has no `broadcaster.templates_setup` module, use this simpler check:

```bash
python -c "
import os
os.environ.setdefault('DATABASE_URL', '/tmp/dash-check.db')
os.environ.setdefault('SESSION_SECRET', 'x'*40)
os.environ.setdefault('IP_HASH_PEPPER', 'x')
os.environ.setdefault('ADMIN_PASSWORD', 'x')
from app import templates
tmpl = templates.env.get_template('admin/dashboard.html')
print(tmpl.render(app_name='X', overview={'kpis':{'users_total':0,'users_active':0,'users_new_week':0,'broadcasts_total':0,'views_week':0,'comments_week':0,'pending_mod':0}, 'views_by_day':[], 'recent_broadcasts':[], 'pending_comments':[]}, active_nav='dashboard', admin={'username':'a'})[:200])
"
```

Expected: prints the first 200 chars of rendered HTML (should start with `<!DOCTYPE` or whatever `base.html` opens with).

If it errors, the most common cause is missing Jinja env context — debug by inspecting the traceback.

- [ ] **Step 4: Commit**

```bash
git add broadcaster/templates/admin/dashboard.html app.py
git commit -m "feat(dashboard): replace placeholder with full dashboard layout"
```

---

## Task 8: Add dashboard CSS

**Files:**
- Modify: `static/css/admin.css` (append at end of file)

- [ ] **Step 1: Append the dashboard block**

Append to the end of `static/css/admin.css`:

```css
/* ── Dashboard ────────────────────────────────────────────── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}
.kpi-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-decoration: none;
  color: inherit;
  transition: transform 120ms ease, box-shadow 120ms ease;
}
.kpi-tile:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}
.kpi-label {
  font-size: 12px;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.kpi-value {
  font-size: 32px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.1;
}
.kpi-sub {
  font-size: 12px;
  color: var(--muted);
}
.kpi-warn {
  background: var(--danger-soft);
  border-color: var(--danger);
}
.kpi-warn .kpi-value {
  color: var(--danger);
}

.dash-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 20px;
  margin-bottom: 20px;
}
.dash-card-wide {
  /* same as .card; class is here so the markup intent is explicit */
}
.pending-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.pending-list li {
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.pending-list li:last-child {
  border-bottom: 0;
}
.quick-links {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

@media (max-width: 1100px) {
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
  .dash-row { grid-template-columns: 1fr; }
}
@media (max-width: 600px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
```

- [ ] **Step 2: Commit**

```bash
git add static/css/admin.css
git commit -m "css(dashboard): add KPI grid, tile, dash-row, pending-list styles"
```

---

## Task 9: Bump admin.css cache buster

**Files:**
- Modify: `broadcaster/templates/base.html` (find the `admin.css?v=7` reference)

- [ ] **Step 1: Find and update the version**

Run:
```bash
grep -n 'admin.css' broadcaster/templates/base.html
```

Expected output: a single line referencing `admin.css?v=7` (or whatever the current value is).

Edit that line, changing `?v=7` to `?v=8`. If the grep shows a different number (e.g., `?v=9`), change it to `?v=N+1`.

- [ ] **Step 2: Commit**

```bash
git add broadcaster/templates/base.html
git commit -m "css: bump admin.css cache-buster to v=8 for dashboard styles"
```

---

## Task 10: Add route integration tests

**Files:**
- Modify: `tests/test_dashboard.py` (append)

- [ ] **Step 1: Append route tests**

Append to `tests/test_dashboard.py`:

```python
# ── /admin/ route ───────────────────────────────────────────


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
    """Fresh DB: KPIs render as 0, fallback copy visible, no crash."""
    r = await authed_client.get("/admin/")
    assert r.status_code == 200
    assert "0</span>" in r.text            # kpi-value rendered for each tile
    assert "No broadcasts yet" in r.text
    assert "No comments to review" in r.text
    assert "all clear" in r.text


async def test_dashboard_seeded_state(authed_client):
    """Seeded data shows real numbers in the rendered HTML."""
    users = [users_svc.create_user(name=f"U{i}", phone=f"72000000{i:02d}")
             for i in range(3)]
    b = bc_svc.create_broadcast(
        title="Promo June", user_ids=[u["id"] for u in users])
    with get_db() as conn:
        link_id = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchone()["id"]
        conn.execute(
            "INSERT INTO link_views (link_id, viewed_at, ip_hash, ua_hash) "
            "VALUES (?, datetime('now'), 'h', 'u')", (link_id,))
        conn.commit()

    r = await authed_client.get("/admin/")
    assert r.status_code == 200
    # KPI tile values: users_total=3, broadcasts_total=1, views_week=1
    assert ">3<" in r.text
    assert ">1<" in r.text
    # Recent broadcasts table shows the broadcast title
    assert "Promo June" in r.text
    # Chart.js script tag and views_by_day JSON are embedded
    assert "cdn.jsdelivr.net" in r.text
    assert '"views":' in r.text
```

- [ ] **Step 2: Run all dashboard tests**

Run:
```bash
pytest tests/test_dashboard.py -v
```

Expected: all 9 tests pass (5 service unit tests from Tasks 2–3 + 4 route integration tests from this task).

- [ ] **Step 3: Commit**

```bash
git add tests/test_dashboard.py
git commit -m "test(dashboard): add /admin/ route integration tests"
```

---

## Task 11: Run full test suite

**Files:** (no changes — verification only)

- [ ] **Step 1: Run the entire suite**

Run:
```bash
pytest -q
```

Expected: all tests pass (existing 175 + 9 new dashboard tests = 184).

If any test fails, STOP and debug. Do not move on with a broken suite.

- [ ] **Step 2: If green, no commit needed (no source changes)**

If you had to fix something, commit the fix:
```bash
git add -A
git commit -m "test: fix dashboard test regressions"
```

---

## Task 12: Manual smoke test

**Files:** (no changes — verification only)

- [ ] **Step 1: Start the dev server**

Run in the background:
```bash
uvicorn app:app --host 0.0.0.0 --port 8123 --reload
```

Wait ~3 seconds for startup.

- [ ] **Step 2: Log in via curl and verify the dashboard renders**

Run:
```bash
# Login + capture session cookie
curl -s -c /tmp/c.txt -X POST http://localhost:8123/api/auth/login \
  -d "username=admin&password=admin1234" \
  -H "Accept: application/json" -o /dev/null

# Fetch dashboard
curl -s -b /tmp/c.txt http://localhost:8123/admin/ | grep -E "Dashboard|Views — last 14 days|To moderate|Quick links" | head -20
```

Expected: at least these strings appear:
- `Dashboard`
- `Views — last 14 days`
- `To moderate`
- `Quick links`

If `password=admin1234` is wrong for your local setup, use the password in your `.env` (`ADMIN_PASSWORD=...`).

- [ ] **Step 3: Visual check via browser**

Open `http://localhost:8123/admin/` in a browser. Verify:

- 6 KPI tiles render in one row at desktop width (≥1100px).
- "To moderate" tile has pink background if any visible comments exist.
- "Views — last 14 days" card shows a line chart (Chart.js loaded from CDN).
- "Recent broadcasts" table is populated (or shows "No broadcasts yet").
- "Pending comments" panel renders the 5 oldest visible comments (or fallback text).
- "Quick links" has 4 buttons stacked vertically.
- Layout collapses gracefully on smaller widths (resize the window).

- [ ] **Step 4: Stop the dev server**

Find the background task and stop it (Ctrl+C in the terminal where it ran, or `pkill -f "uvicorn app:app"`).

- [ ] **Step 5: No commit (verification only)**

---

## Self-review checklist (run before declaring done)

- [ ] All 6 KPI tiles implemented per spec.
- [ ] Chart.js loaded from CDN with `try/catch` fallback to static table.
- [ ] Recent broadcasts uses `.pill .pill.{modifier}` (not invented classes).
- [ ] Pending comments links to `/admin/comments` (no `?focus=`).
- [ ] One new index (`idx_users_created_at`) added; no other schema changes.
- [ ] CSP allows `https://cdn.jsdelivr.net` in `script-src`.
- [ ] Full test suite green (184/184).
- [ ] Browser smoke test confirms layout.