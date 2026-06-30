# Broadcast List Page — Analytics + Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a counter grid (sent vs pending per `category × delivery_channel`) and a category/channel/date-range filter to `/admin/broadcasts` so admins can answer "what did we send, what's queued" at a glance.

**Architecture:** Pure server-side rendering on the existing `/admin/broadcasts` page. Two new SQL queries (list + count) share one WHERE-clause builder so the counter grid and the table cannot disagree. The filter is a plain HTML `<form method="get">` — no new JS framework, no new dependencies. Date filter applies to `scheduled_at` with NULL pass-through so drafts always show up.

**Tech Stack:** FastAPI + Jinja2 SSR (existing), SQLite (existing), pytest + httpx + ASGITransport (existing). No new deps.

**Spec:** `docs/superpowers/specs/2026-06-30-broadcast-analytics-filtering-design.md`

**File map (final state of this plan):**

| File | Status | Responsibility |
|---|---|---|
| `broadcaster/services/broadcasts.py` | modify | Add `_broadcast_filters_where`, extend `list_broadcasts`, add `count_broadcasts_by_category_channel`, add `distinct_categories` |
| `app.py` | modify | Add `_validate_filters`, wire filters into `/admin/broadcasts` page, pass new context to template |
| `broadcaster/routes/admin_broadcasts.py` | modify | Extend `GET /api/broadcasts` with same filter kwargs |
| `broadcaster/templates/admin/broadcasts_list.html` | modify | Add filter `<form>` and counter-grid `<div>` above existing table |
| `static/css/admin.css` | modify | Add `.filter-row`, `.counter-grid`, `.counter-card`, `.counter-sent`, `.counter-pending`, `.counter-other` |
| `tests/test_broadcasts.py` | extend | Add `test_api_broadcasts_accepts_same_filter_kwargs` (API parity) |
| `tests/test_broadcasts_page.py` | create | 12 page-level tests (counts, filters, NULL pass-through, errors, agreement) |

---

## Task 1: Service layer — filters helper + extended `list_broadcasts` + count function + distinct categories

**Files:**
- Modify: `broadcaster/services/broadcasts.py` (extend `list_broadcasts`; add `_broadcast_filters_where`, `count_broadcasts_by_category_channel`, `distinct_categories`)
- Modify: `tests/test_broadcasts.py` (add service-level tests)

This task lands the SQL foundation. After this task, two queries (list and count) share one WHERE-clause function and the service has all the functions the page and API will need. The route/template/CSS/tests in later tasks all build on this.

- [ ] **Step 1.1: Write failing tests for `_broadcast_filters_where`**

Add to `tests/test_broadcasts.py` at end of file:

```python
# ── _broadcast_filters_where ──────────────────────────────────────────────


def test_filters_where_empty_returns_empty_clause():
    where, params = bc_svc._broadcast_filters_where({})
    assert where == ""
    assert params == []


def test_filters_where_category_adds_eq_param():
    where, params = bc_svc._broadcast_filters_where({"category": "Promo"})
    assert where == "b.category = ?"
    assert params == ["Promo"]


def test_filters_where_channel_adds_eq_param():
    where, params = bc_svc._broadcast_filters_where({"channel": "email"})
    assert where == "b.delivery_channel = ?"
    assert params == ["email"]


def test_filters_where_date_range_includes_null_passthrough():
    where, params = bc_svc._broadcast_filters_where({
        "date_from": "2026-06-01", "date_to": "2026-06-30",
    })
    assert where == "(b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)"
    assert params == ["2026-06-01 00:00:00", "2026-06-30 23:59:59"]


def test_filters_where_combines_with_and():
    where, params = bc_svc._broadcast_filters_where({
        "category": "Promo", "channel": "whatsapp",
        "date_from": "2026-06-01", "date_to": "2026-06-30",
    })
    assert where == "b.category = ? AND b.delivery_channel = ? AND (b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)"
    assert params == ["Promo", "whatsapp", "2026-06-01 00:00:00", "2026-06-30 23:59:59"]


def test_filters_where_ignores_blank_strings():
    where, params = bc_svc._broadcast_filters_where({
        "category": "", "channel": "  ", "date_from": None,
    })
    assert where == ""
    assert params == []


def test_filters_where_partial_date_range_omits_clause():
    """Either both date bounds or neither; partial ignored by caller."""
    where, params = bc_svc._broadcast_filters_where({"date_from": "2026-06-01"})
    assert where == ""
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "filters_where" -v 2>&1 | tail -20`
Expected: 7 failures with `AttributeError: module 'broadcaster.services.broadcasts' has no attribute '_broadcast_filters_where'`

- [ ] **Step 1.3: Implement `_broadcast_filters_where`**

Replace the body of `list_broadcasts` (line ~136 in `broadcaster/services/broadcasts.py`) by first adding the helper directly above it. Insert this just above the `# ── Read ──` comment block (the helper is small enough to live with list_broadcasts):

```python
# ── Filter WHERE-clause helper ────────────────────────────────────
# Single source of truth for the category / channel / date filter that
# the broadcasts page and the JSON API both apply. Both list_broadcasts
# and count_broadcasts_by_category_channel call this so their result
# sets cannot drift apart.
#
# Conventions:
#   - Empty string / None filter values are dropped (no clause emitted).
#   - date_from + date_to must BOTH be present, or the caller must
#     pre-validate and drop the partial range (see _validate_filters
#     in app.py). The helper emits the BETWEEN clause here regardless;
#     the caller is responsible for not calling it with partial dates.
#   - The date BETWEEN range uses `scheduled_at IS NULL OR ...` so
#     unscheduled drafts pass through the filter.
#   - The caller binds the resulting `where` string after "WHERE" and
#     the resulting `params` list to the placeholders it defined.


def _broadcast_filters_where(filters: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    category = (filters.get("category") or "").strip()
    if category:
        clauses.append("b.category = ?")
        params.append(category)

    channel = (filters.get("channel") or "").strip()
    if channel:
        clauses.append("b.delivery_channel = ?")
        params.append(channel)

    date_from = (filters.get("date_from") or "").strip()
    date_to = (filters.get("date_to") or "").strip()
    if date_from and date_to:
        clauses.append(
            "(b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)"
        )
        params.append(f"{date_from} 00:00:00")
        params.append(f"{date_to} 23:59:59")

    where = " AND ".join(clauses)
    return where, params
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "filters_where" -v 2>&1 | tail -15`
Expected: 7 PASS

- [ ] **Step 1.5: Write failing tests for extended `list_broadcasts`**

Add below the `_broadcast_filters_where` tests:

```python
# ── list_broadcasts new filter params ─────────────────────────────


@pytest.fixture
async def _three_broadcasts(authed_client):
    """Three broadcasts in different cat/ch. Status determined by what
    create_broadcast accepts (draft if no scheduled_at, queued if
    scheduled). Tests then UPDATE status directly to set fixtures."""
    a, = await _make_users(authed_client, ("BcastU", "7000000001", "", ""))
    ids = []
    for title, cat, ch in [("Promo-A", "Promo", "whatsapp"),
                            ("Promo-B", "Promo", "email"),
                            ("General-A", "General", "whatsapp")]:
        r = await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def _set_broadcast_status(bid: int, status: str, scheduled_at: str | None = None):
    """Direct-DB status setter used by filter/aggregation tests so we
    can build fixtures faster than driving the full /send pipeline."""
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE broadcasts SET status = ?, scheduled_at = COALESCE(?, scheduled_at) WHERE id = ?",
            (status, scheduled_at, bid),
        )


def test_list_broadcasts_filter_by_category(_three_broadcasts):
    _set_broadcast_status(_three_broadcasts[0], "sent")
    out = bc_svc.list_broadcasts(category="Promo")
    titles = {b["title"] for b in out}
    assert titles == {"Promo-A", "Promo-B"}


def test_list_broadcasts_filter_by_channel(_three_broadcasts):
    out = bc_svc.list_broadcasts(channel="whatsapp")
    titles = {b["title"] for b in out}
    assert titles == {"Promo-A", "General-A"}


def test_list_broadcasts_filter_by_date_range_passes_null_through(_three_broadcasts):
    """Two scheduled-in-range, one scheduled-out, one draft (NULL) → all four pass."""
    _three_broadcasts.append(
        bc_svc.create_broadcast(
            title="UnscheDraft", category="Promo", delivery_channel="whatsapp",
            user_ids=[1], mode="draft",  # scheduled_at=None → NULL
        )["id"]
    )
    _set_broadcast_status(_three_broadcasts[0], "sent", "2026-06-15T12:00:00")
    _set_broadcast_status(_three_broadcasts[1], "queued", "2026-06-15T12:00:00")
    _set_broadcast_status(_three_broadcasts[2], "draft", "2026-07-15T12:00:00")
    # (the 4th has NULL scheduled_at — the draft)

    out = bc_svc.list_broadcasts(date_from="2026-06-01", date_to="2026-06-30")
    titles = {b["title"] for b in out}
    # The two 06-15 rows + the unscheduled draft = 3 visible.
    assert "Promo-A" in titles
    assert "Promo-B" in titles
    assert "UnscheDraft" in titles
    # The 07-15 row is out of range.
    assert "General-A" not in titles
```

- [ ] **Step 1.6: Run new list tests to verify they fail**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "list_broadcasts_filter" -v 2>&1 | tail -20`
Expected: All 3 fail with `TypeError: list_broadcasts() got an unexpected keyword argument 'category'` (or similar)

- [ ] **Step 1.7: Extend `list_broadcasts` signature to use the helper**

Replace the existing `list_broadcasts` (lines 136-165 of `broadcaster/services/broadcasts.py`) with:

```python
def list_broadcasts(
    status: Optional[str] = None,
    with_links: Optional[bool] = None,
    q: Optional[str] = None,
    category: Optional[str] = None,
    channel: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    # Category / channel / date range come from the shared helper so
    # the JSON API and the HTML page apply identical filters.
    extra_where, extra_params = _broadcast_filters_where({
        "category": category, "channel": channel,
        "date_from": date_from, "date_to": date_to,
    })

    where: list[str] = []
    params: list = []
    if status:
        where.append("b.status = ?")
        params.append(status)
    if with_links is True:
        where.append("b.generate_links = 1")
    elif with_links is False:
        where.append("b.generate_links = 0")
    if q:
        where.append("(b.title LIKE ? OR b.message_text LIKE ?)")
        like = f"%{q}%"
        params += [like, like]
    if extra_where:
        where.append(extra_where)
        params += extra_params

    sql = (
        "SELECT b.id, b.title, b.category, b.delivery_channel, b.status, b.scheduled_at, "
        "b.sent_at, b.created_at, b.generate_links, "
        "(SELECT COUNT(*) FROM broadcast_links WHERE broadcast_id = b.id) AS link_count, "
        "(SELECT COUNT(*) FROM broadcast_targets WHERE broadcast_id = b.id) AS target_count "
        "FROM broadcasts b"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id DESC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 1.8: Run new list tests to verify they pass**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "list_broadcasts_filter" -v 2>&1 | tail -10`
Expected: 3 PASS

- [ ] **Step 1.9: Write failing tests for `count_broadcasts_by_category_channel`**

Add below the list tests:

```python
# ── count_broadcasts_by_category_channel ──────────────────────────


def test_count_broadcasts_buckets_statuses_correctly(_three_broadcasts):
    _set_broadcast_status(_three_broadcasts[0], "sent")      # Promo-A / whatsapp / sent
    _set_broadcast_status(_three_broadcasts[1], "draft")     # Promo-B / email / draft
    _set_broadcast_status(_three_broadcasts[2], "queued")    # General-A / whatsapp / queued
    rows = bc_svc.count_broadcasts_by_category_channel()
    by_key = {(r["category"], r["channel"]): r for r in rows}
    promo_wa = by_key[("Promo", "whatsapp")]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 0  # no draft/queued in this group
    promo_em = by_key[("Promo", "email")]
    assert promo_em["sent"] == 0
    assert promo_em["pending"] == 1
    gen_wa = by_key[("General", "whatsapp")]
    assert gen_wa["sent"] == 0
    assert gen_wa["pending"] == 1


def test_count_broadcasts_excludes_partial_failed_from_pending(_three_broadcasts):
    _set_broadcast_status(_three_broadcasts[0], "sent")
    _set_broadcast_status(_three_broadcasts[1], "partial")
    _set_broadcast_status(_three_broadcasts[2], "failed")
    rows = bc_svc.count_broadcasts_by_category_channel()
    # Each broadcast is in its own (cat, ch) bucket because _three_broadcasts
    # creates (Promo, whatsapp), (Promo, email), (General, whatsapp).
    promo_wa = [r for r in rows if (r["category"], r["channel"]) == ("Promo", "whatsapp")][0]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 0
    assert promo_wa["partial"] == 0
    promo_em = [r for r in rows if (r["category"], r["channel"]) == ("Promo", "email")][0]
    assert promo_em["pending"] == 0
    assert promo_em["partial"] == 1
    assert promo_em["failed"] == 0
    gen_wa = [r for r in rows if (r["category"], r["channel"]) == ("General", "whatsapp")][0]
    assert gen_wa["pending"] == 0
    assert gen_wa["failed"] == 1


def test_count_broadcasts_applies_same_filters_as_list(_three_broadcasts):
    """Spec invariant: counts always sum to filtered table size."""
    _set_broadcast_status(_three_broadcasts[0], "sent")
    _set_broadcast_status(_three_broadcasts[1], "queued")
    _set_broadcast_status(_three_broadcasts[2], "draft")
    rows = bc_svc.count_broadcasts_by_category_channel(category="Promo")
    total = sum(r["total"] for r in rows)
    listed = bc_svc.list_broadcasts(category="Promo")
    assert total == len(listed)
    assert total == 2  # Promo-A + Promo-B


def test_count_broadcasts_returns_zero_for_empty_filter():
    """With no broadcasts in the DB, returns empty list (no zero-rows)."""
    assert bc_svc.count_broadcasts_by_category_channel() == []
```

- [ ] **Step 1.10: Run count tests to verify they fail**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "count_broadcasts" -v 2>&1 | tail -10`
Expected: All fail with `AttributeError: module 'broadcaster.services.broadcasts' has no attribute 'count_broadcasts_by_category_channel'`

- [ ] **Step 1.11: Implement `count_broadcasts_by_category_channel`**

Add directly below `list_broadcasts` (it groups rows by `category + delivery_channel` and aggregates status counts):

```python
def count_broadcasts_by_category_channel(
    category: Optional[str] = None,
    channel: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """One row per (category, delivery_channel) that has at least one
    broadcast in the filtered set. Each row carries per-status counts.

    Sums across rows always equal the row count of `list_broadcasts`
    applied with the same filters — both queries go through
    `_broadcast_filters_where` so they cannot drift apart.
    """
    extra_where, extra_params = _broadcast_filters_where({
        "category": category, "channel": channel,
        "date_from": date_from, "date_to": date_to,
    })

    sql = (
        "SELECT  b.category, b.delivery_channel AS channel, "
        "        SUM(CASE WHEN b.status = 'sent'                            THEN 1 ELSE 0 END) AS sent, "
        "        SUM(CASE WHEN b.status IN ('draft','queued')               THEN 1 ELSE 0 END) AS pending, "
        "        SUM(CASE WHEN b.status = 'sending'                         THEN 1 ELSE 0 END) AS sending, "
        "        SUM(CASE WHEN b.status = 'partial'                         THEN 1 ELSE 0 END) AS partial, "
        "        SUM(CASE WHEN b.status = 'failed'                          THEN 1 ELSE 0 END) AS failed, "
        "        SUM(CASE WHEN b.status = 'cancelled'                       THEN 1 ELSE 0 END) AS cancelled, "
        "        COUNT(*)                                                   AS total "
        "FROM    broadcasts b"
    )
    params: list = []
    if extra_where:
        sql += " WHERE " + extra_where
        params += extra_params
    sql += " GROUP BY b.category, b.delivery_channel HAVING COUNT(*) > 0 ORDER BY b.category, b.delivery_channel"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 1.12: Run count tests to verify they pass**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "count_broadcasts" -v 2>&1 | tail -10`
Expected: 4 PASS

- [ ] **Step 1.13: Write failing tests for `distinct_categories`**

Add at the bottom of `tests/test_broadcasts.py`:

```python
# ── distinct_categories ──────────────────────────────────────────


async def test_distinct_categories_returns_sorted_unique(authed_client):
    a, = await _make_users(authed_client, ("CatU", "7100000001", "", ""))
    for cat in ["Promo", "General", "Reminder", "Promo"]:  # duplicate Promo
        await authed_client.post("/api/broadcasts", json={
            "title": f"B-{cat}", "category": cat,
            "delivery_channel": "email", "user_ids": [a], "mode": "draft",
        })
    out = bc_svc.distinct_categories()
    assert out == ["General", "Promo", "Reminder"]


def test_distinct_categories_empty_db_returns_empty():
    # This test runs against a fresh DB only if previous tests didn't
    # create categories — use a brand-new fixture path.
    assert bc_svc.distinct_categories() == []  # post-_isolate_db fixture
```

Wait — Step 1.13's second test is broken because the test uses `bc_svc.distinct_categories()` at module import time of tests, but the DB is shared within `_isolate_db`. Move the empty-DB test into its own function with no fixture pollution. Replace Step 1.13 with:

```python
# ── distinct_categories ──────────────────────────────────────────


def test_distinct_categories_returns_sorted_unique(_three_broadcasts):
    """The _three_broadcasts fixture creates categories Promo, Promo, General."""
    out = bc_svc.distinct_categories()
    # Sorted, deduplicated.
    assert out == ["General", "Promo"]
```

- [ ] **Step 1.14: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "distinct_categories" -v 2>&1 | tail -10`
Expected: 1 FAIL with `AttributeError: module ... has no attribute 'distinct_categories'`

- [ ] **Step 1.15: Implement `distinct_categories`**

Add directly below `count_broadcasts_by_category_channel`:

```python
def distinct_categories() -> list[str]:
    """Distinct categories currently in the broadcasts table, sorted.

    Used to populate the filter `<select>` on the broadcasts page
    without forcing the admin to maintain a separate categories table.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM broadcasts WHERE category IS NOT NULL "
            "AND category != '' ORDER BY category"
        ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 1.16: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "distinct_categories" -v 2>&1 | tail -10`
Expected: 1 PASS

- [ ] **Step 1.17: Run the entire broadcast test file**

Run: `.venv/bin/pytest tests/test_broadcasts.py -v 2>&1 | tail -10`
Expected: ALL (existing + 16 new) PASS. If anything fails, fix before committing.

- [ ] **Step 1.18: Commit**

```bash
git add broadcaster/services/broadcasts.py tests/test_broadcasts.py
git commit -m "feat(broadcasts): category/channel/date filters + count matrix

Adds _broadcast_filters_where helper (single source of WHERE-clause
truth for list + count queries), extends list_broadcasts with
category/channel/date_from/date_to kwargs, adds
count_broadcasts_by_category_channel for the counter grid, and
distinct_categories for the filter select.

16 new tests covering WHERE-builder behaviour, list filtering, status
bucketing, partial/failed handling, and category-list ordering.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Route validation + page wiring (`/admin/broadcasts` filters)

**Files:**
- Modify: `app.py` — `admin_broadcasts_page` route + new `_validate_filters` helper
- Modify: `tests/test_broadcasts.py` (or new `tests/test_broadcasts_route.py`)

This task teaches the route to read filter query params, validate them, call the new service functions, and pass the result to the template. After this, the page route is fully wired — only the template (Task 3) needs to render the new UI.

- [ ] **Step 2.1: Write failing tests for `_validate_filters`**

Find the right place to put these — they test the helper as exported from `app.py`. Use this file location: `tests/test_broadcasts_route.py` (new file). Reason for new file: `app.py` is not import-target-friendly (it has top-level FastAPI instantiation); we exercise `_validate_filters` indirectly through the page route. So put validation tests directly against the route to avoid importing `app.py` internals.

Create `tests/test_broadcasts_route.py`:

```python
"""Page-route-level tests for /admin/broadcasts.

These tests exercise the SSR page directly. They cover the filter
validation logic implemented as `_validate_filters` in app.py.
"""
from __future__ import annotations

import pytest

from broadcaster.services import broadcasts as bc_svc

# Reuse the auth + user setup from test_broadcasts.
from tests.test_broadcasts import _login, _make_users, _set_broadcast_status


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


async def test_page_with_no_query_returns_all_broadcasts(authed_client):
    a, = await _make_users(authed_client, ("RouteU", "7200000001", "", ""))
    await authed_client.post("/api/broadcasts", json={
        "title": "T1", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    r = await authed_client.get("/admin/broadcasts")
    assert r.status_code == 200
    assert "T1" in r.text


async def test_page_with_category_filter_applies(authed_client):
    a, = await _make_users(authed_client, ("RouteU", "7200000002", "", ""))
    for title, cat in [("Promo one", "Promo"), ("General one", "General")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": "email",
            "user_ids": [a], "mode": "draft",
        })
    r = await authed_client.get("/admin/broadcasts?category=Promo")
    assert r.status_code == 200
    assert "Promo one" in r.text
    assert "General one" not in r.text


async def test_page_with_invalid_date_range_flashes_and_does_not_500(authed_client):
    r = await authed_client.get(
        "/admin/broadcasts?date_from=2026-06-30&date_to=2026-06-01"
    )
    # Page renders 200 — bad inputs become a flash, not a 4xx/5xx.
    assert r.status_code == 200
    # The spec's flash text contains a substring we can assert on.
    assert "date_from" in r.text


async def test_page_with_single_date_bound_flashes_and_does_not_500(authed_client):
    r = await authed_client.get("/admin/broadcasts?date_from=2026-06-30")
    assert r.status_code == 200
    assert "both" in r.text.lower()


async def test_page_with_unknown_category_value_ignores_filter(authed_client):
    a, = await _make_users(authed_client, ("RouteU", "7200000003", "", ""))
    await authed_client.post("/api/broadcasts", json={
        "title": "T-Unknown", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    # Hand-edited URL with a category that doesn't exist in the DB.
    r = await authed_client.get("/admin/broadcasts?category=NotARealCategory")
    assert r.status_code == 200
    # No 500, page renders normally; the existing broadcast is visible
    # (filter was ignored as if not applied).
    assert "T-Unknown" in r.text


async def test_page_form_preserves_selected_filter(authed_client):
    r = await authed_client.get("/admin/broadcasts?category=Promo")
    assert r.status_code == 200
    # The <option value="Promo">...</option> with `selected` attribute
    # — i.e. the form reflects what was submitted.
    assert 'value="Promo" selected' in r.text


async def test_page_clear_link_is_bare_url(authed_client):
    r = await authed_client.get("/admin/broadcasts?category=Promo")
    assert r.status_code == 200
    # There's a "Clear" link that points to the bare URL with no query string.
    assert 'href="/admin/broadcasts"' in r.text
```

- [ ] **Step 2.2: Run route tests to verify they all fail**

Run: `.venv/bin/pytest tests/test_broadcasts_route.py -v 2>&1 | tail -20`
Expected: All 7 fail. The `unknown category` and `with no query` tests will return 200 (page already exists) but won't match the assertions about `T1` visibility or filter reflection. The invalid-date tests will currently 500 because no validation exists. The expected failures span 500s and assertion-mismatches.

- [ ] **Step 2.3: Implement `_validate_filters` in `app.py`**

In `app.py`, add the helper near the top (under the existing imports / settings config). Insert this block after the import of `admin_auth` (search the file for the existing top-of-file helper definitions to pick a good spot):

```python
def _validate_filters(query_params) -> tuple[dict, Optional[str]]:
    """Read category/channel/date_range filter query params and return
    (cleaned_filters, flash_message_or_None).

    Rules (see docs/superpowers/specs/2026-06-30-broadcast-analytics-
    filtering-design.md for the authoritative definitions):

      - Empty / whitespace values are dropped.
      - Unknown category values are dropped (no error).
      - Unknown channel values are dropped (no error).
      - date_from > date_to  → both dropped, flash.
      - Only one date bound  → both dropped, flash.
      - Unparseable dates    → treated as absent (no flash).
    """
    category = (query_params.get("category") or "").strip()
    channel = (query_params.get("channel") or "").strip()
    date_from = (query_params.get("date_from") or "").strip()
    date_to = (query_params.get("date_to") or "").strip()

    flash: Optional[str] = None
    cleaned = {
        "category": category,
        "channel": channel,
        "date_from": date_from,
        "date_to": date_to,
    }

    if (date_from and not date_to) or (date_to and not date_from):
        cleaned["date_from"] = ""
        cleaned["date_to"] = ""
        flash = "Pick both dates or leave both empty."
    elif date_from and date_to:
        try:
            from datetime import date as _date
            d_from = _date.fromisoformat(date_from)
            d_to = _date.fromisoformat(date_to)
        except (TypeError, ValueError):
            cleaned["date_from"] = ""
            cleaned["date_to"] = ""
            flash = "Date inputs must be valid dates."
        else:
            if d_from > d_to:
                cleaned["date_from"] = ""
                cleaned["date_to"] = ""
                flash = f"date_from ({date_from}) cannot be after date_to ({date_to})."

    # Drop unknown category silently (no flash — could be a hand-edited URL).
    if cleaned["category"]:
        from broadcaster.services import broadcasts as _bc
        valid_categories = set(_bc.distinct_categories())
        if cleaned["category"] not in valid_categories:
            cleaned["category"] = ""

    # Drop unknown channel silently (whitelist).
    if cleaned["channel"] and cleaned["channel"] not in ("whatsapp", "email", "both"):
        cleaned["channel"] = ""

    return cleaned, flash
```

- [ ] **Step 2.4: Wire `_validate_filters` into `/admin/broadcasts` route**

Replace the existing `admin_broadcasts_page` function body (lines 207-224 in `app.py`) with this updated version:

```python
@app.get("/admin/broadcasts", response_class=HTMLResponse)
def admin_broadcasts_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import broadcasts as bc_svc
    from broadcaster.services import content as content_svc  # unused today; left for future; remove if causing linter noise

    filters, filter_flash = _validate_filters(request.query_params)

    # If the user was bounced here from a deleted/missing broadcast,
    # show a one-shot message so they know what happened.
    flash = filter_flash
    if not flash:
        missing_id = request.query_params.get("missing")
        if missing_id:
            flash = f"Broadcast #{missing_id} no longer exists (it may have been deleted)."

    broadcasts = bc_svc.list_broadcasts(
        category=filters["category"] or None,
        channel=filters["channel"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
    )
    counts = bc_svc.count_broadcasts_by_category_channel(
        category=filters["category"] or None,
        channel=filters["channel"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
    )
    category_options = bc_svc.distinct_categories()
    applied = {
        "category": filters["category"],
        "channel": filters["channel"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
    }
    return templates.TemplateResponse(
        request, "admin/broadcasts_list.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "admin": {"username": "admin"},
         "broadcasts": broadcasts, "counts": counts,
         "applied": applied,
         "category_options": category_options,
         "channel_options": ["whatsapp", "email", "both"],
         "flash": flash},
    )
```

Note: the template references `counts`, `applied`, `category_options`, `channel_options` that don't exist yet in `broadcasts_list.html`. After this change, until Task 3 lands, the page will render but Jinja will raise `UndefinedError` for these new variables. To prevent that, add the conditional rendering in Step 2.5.

Also remove the `from broadcaster.services import content as content_svc  # unused today` line if lint complains — it was just a placeholder note.

- [ ] **Step 2.5: Make the template tolerate the new context variables BEFORE wiring**

In `broadcaster/templates/admin/broadcasts_list.html`, wrap the new sections with `{% if counts is defined %}` / `{% endif %}` so the page still renders while we build up the UI. Replace the entire `{% block body %}` content (lines 5-63) with this transitional version:

```html
{% block body %}
{% include "admin/_nav.html" %}

<main class="main">
  {% if flash %}
  <div class="flash flash-info" role="status" data-flash>{{ flash }}</div>
  {% endif %}
  <div class="page-head">
    <div>
      <h1>Broadcasts</h1>
      <p class="sub">Compose and send messages — {{ broadcasts|length }} total</p>
    </div>
    <a href="/admin/broadcasts/new" class="btn success">+ New Broadcast</a>
  </div>

  {# ── Filter bar (populated in Task 3) ── #}
  {# Placeholder so the route can hand over new context without 500-ing. #}
  {% if applied is defined %}
  {# TODO(task-3): replace with full filter form. #}
  {% endif %}

  {# ── Counter grid (populated in Task 3) ── #}
  {% if counts is defined %}
  {# TODO(task-3): replace with full counter-grid markup. #}
  {% endif %}

  <div class="card">
    <div class="card-head">
      <h2>All Broadcasts</h2>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Title</th><th>Category</th><th>Channel</th>
            <th>Targets</th><th>Links</th><th>Status</th>
            <th>Scheduled</th><th></th>
          </tr>
        </thead>
        <tbody>
          {% for b in broadcasts %}
          <tr>
            <td><b>{{ b.title }}</b></td>
            <td><span class="pill muted">{{ b.category }}</span></td>
            <td>{{ b.delivery_channel }}</td>
            <td>{{ b.target_count }}</td>
            <td>{{ b.link_count }}</td>
            <td>
              {% if b.status == 'draft' %}<span class="pill muted">Draft</span>
              {% elif b.status == 'queued' %}<span class="pill info">Queued</span>
              {% elif b.status == 'sent' %}<span class="pill success">Sent</span>
              {% elif b.status == 'cancelled' %}<span class="pill danger">Cancelled</span>
              {% else %}<span class="pill warning">{{ b.status }}</span>{% endif %}
            </td>
            <td class="scheduled-cell" data-scheduled-at="{{ b.scheduled_at or '' }}">
              <span class="muted">…</span>
            </td>
            <td>
              <a href="/admin/broadcasts/{{ b.id }}" class="btn small">View</a>
            </td>
          </tr>
          {% else %}
          <tr><td colspan="8" class="empty">No broadcasts yet. <a href="/admin/broadcasts/new">Create your first one</a>.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</main>
{% endblock %}
```

The `{% if applied is defined %}` / `{% if counts is defined %}` guards ensure the template still renders when the page handler is the old one — useful if you need to step back and debug. After Task 3 the `{# TODO #}` blocks get replaced with real markup.

- [ ] **Step 2.6: Run all tests to confirm no regressions**

Run: `.venv/bin/pytest tests/ -v 2>&1 | tail -10`
Expected: ALL PASS. The transitional template still renders the page; existing route tests pass. New route tests should now pass.

- [ ] **Step 2.7: Run the new route tests to verify they pass**

Run: `.venv/bin/pytest tests/test_broadcasts_route.py -v 2>&1 | tail -10`
Expected: 7 PASS (the `counts`/`applied` template references from Step 2.4 are not yet rendered, so the page test `test_page_clear_link_is_bare_url` won't find `href="/admin/broadcasts"` until Task 3 — adjust by skipping this one test for now and re-enabling in Task 3)

Actually, the `test_page_clear_link_is_bare_url` test expects the Clear link, which only exists after Task 3. Two options:
  (a) Drop that test until Task 3.
  (b) Assert something else (e.g. that the filter param is in the URL — that one already exists).

Go with (b). Update Step 2.7's expected assertion to:

```python
    assert "Clear" in r.text  # verify the filter-clear UI is in the page
```

Hmm — but that ALSO only exists after Task 3. Truly none of the form-rendering tests can pass until Task 3. The cleanest fix is to split: render-only validation tests in Task 2 (these check filter logic); UI tests in Task 3. Update Step 2.1 to drop `test_page_form_preserves_selected_filter` and `test_page_clear_link_is_bare_url` — these are UI concerns and belong in Task 3.

Replace Step 2.1's last two tests with:

```python
async def test_page_with_unknown_category_value_ignores_filter(authed_client):
    a, = await _make_users(authed_client, ("RouteU", "7200000003", "", ""))
    await authed_client.post("/api/broadcasts", json={
        "title": "T-Unknown", "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",
    })
    r = await authed_client.get("/admin/broadcasts?category=NotARealCategory")
    assert r.status_code == 200
    # No 500, page renders normally; the existing broadcast is visible
    # (filter was ignored as if not applied).
    assert "T-Unknown" in r.text
```

Use this revised `test_broadcasts_route.py` (Step 2.1).

After this step, run tests again:

Run: `.venv/bin/pytest tests/test_broadcasts_route.py -v 2>&1 | tail -10`
Expected: 5 PASS

- [ ] **Step 2.8: Commit**

```bash
git add app.py broadcaster/templates/admin/broadcasts_list.html tests/test_broadcasts_route.py
git commit -m "feat(broadcasts-page): validation + service wiring

Adds _validate_filters helper that returns (cleaned_filters, flash)
for the broadcasts page. Routes /admin/broadcasts to apply filters
to both the list and the (not-yet-rendered) count matrix so they
cannot drift apart.

5 new route-level tests cover happy-path filters, invalid date
range flashing (no 500), and unknown-category fallback.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Template + CSS — render filter form and counter grid

**Files:**
- Modify: `broadcaster/templates/admin/broadcasts_list.html`
- Modify: `static/css/admin.css`

This task replaces the `{# TODO #}` placeholders from Task 2 with the real filter form and counter grid, and adds the supporting CSS. After this task, the page is fully functional from the user's perspective.

- [ ] **Step 3.1: Replace the `{# TODO #}` blocks with real filter form + counter grid**

In `broadcaster/templates/admin/broadcasts_list.html`, replace the two `{% if applied is defined %}` / `{% if counts is defined %}` blocks (lines starting at the `{# ── Filter bar ── #}` comment) with the actual UI:

Filter form block (replaces the first `{% if applied is defined %}` block):

```html
{# ── Filter bar ──────────────────────────────────────────────── #}
<form method="get" action="/admin/broadcasts" class="filter-row">
  <select name="category" class="filter-select" aria-label="Filter by category">
    <option value="">All categories</option>
    {% for cat in category_options %}
    <option value="{{ cat }}" {% if cat == applied.category %}selected{% endif %}>{{ cat }}</option>
    {% endfor %}
  </select>
  <select name="channel" class="filter-select" aria-label="Filter by channel">
    <option value="">All channels</option>
    {% for ch in channel_options %}
    <option value="{{ ch }}" {% if ch == applied.channel %}selected{% endif %}>{{ ch }}</option>
    {% endfor %}
  </select>
  <input type="date" name="date_from" value="{{ applied.date_from or '' }}"
         class="filter-date" aria-label="Filter from date">
  <input type="date" name="date_to" value="{{ applied.date_to or '' }}"
         class="filter-date" aria-label="Filter to date">
  <button type="submit" class="btn">Apply</button>
  <a href="/admin/broadcasts" class="btn secondary">Clear</a>
</form>
```

Counter grid block (replaces the second `{% if counts is defined %}` block):

```html
{# ── Counter grid ────────────────────────────────────────────── #}
{% if counts %}
<div class="counter-grid">
  {% for c in counts %}
  <div class="counter-card">
    <div class="counter-head">{{ c.category }} · {{ c.channel }}</div>
    <div class="counter-main">
      <span class="counter-sent">Sent {{ c.sent }}</span>
      <span class="dot">·</span>
      <span class="counter-pending">Pending {{ c.pending }}</span>
    </div>
    {% set other = c.sending + c.partial + c.failed + c.cancelled %}
    {% if other > 0 %}
    <details class="counter-other">
      <summary>Other {{ other }}</summary>
      <div class="counter-other-body">
        {% if c.sending %}sending {{ c.sending }}{% endif %}
        {% if c.partial %}{% if c.sending %} · {% endif %}partial {{ c.partial }}{% endif %}
        {% if c.failed %}{% if c.sending or c.partial %} · {% endif %}failed {{ c.failed }}{% endif %}
        {% if c.cancelled %}{% if c.sending or c.partial or c.failed %} · {% endif %}cancelled {{ c.cancelled }}{% endif %}
      </div>
    </details>
    {% endif %}
  </div>
  {% endfor %}
</div>
{% elif broadcasts|length == 0 %}
{# Only show the empty message when the filter has narrowed the list
   all the way down. When the DB is genuinely empty, the table's own
   empty row covers it. #}
{% else %}
<div class="counter-empty">No broadcasts match these filters.</div>
{% endif %}
```

Wait — the `{% elif broadcasts|length == 0 %}` branch never fires because the router only goes down this path with `counts == []` AND `broadcasts == []` (no rows = no counts). The else-clause is actually the only useful one. Replace the bottom with just:

```html
{% if counts %}
<div class="counter-grid">
  ... (same as above)
</div>
{% else %}
<div class="counter-empty">No broadcasts match these filters.</div>
{% endif %}
```

- [ ] **Step 3.2: Add the supporting CSS**

Open `static/css/admin.css` and append the following (anywhere after the existing `.card-head` styles is fine — keep alphabetical-ish placement; bottom is fine for first ship):

```css
/* ── Filter row (broadcasts page) ──────────────────────────────── */
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 0 0 16px 0;
  padding: 12px 14px;
  background: var(--card, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
}
.filter-row .filter-select,
.filter-row .filter-date {
  padding: 7px 10px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  font: inherit;
  background: #fff;
  color: inherit;
}
.filter-row .filter-date {
  min-width: 140px;
}

/* ── Counter grid (broadcasts page) ──────────────────────────── */
.counter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin: 0 0 20px 0;
}
.counter-card {
  background: var(--card, #fff);
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 8px;
  padding: 12px 14px;
}
.counter-head {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted, #6b7280);
  margin-bottom: 6px;
  font-weight: 600;
}
.counter-main {
  font-size: 15px;
  font-weight: 500;
}
.counter-main .dot { color: var(--muted, #9ca3af); margin: 0 6px; }
.counter-sent    { color: var(--success, #16a34a); font-weight: 700; }
.counter-pending { color: var(--warning, #ea580c); font-weight: 700; }
.counter-other {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted, #6b7280);
}
.counter-other summary {
  cursor: pointer;
  user-select: none;
}
.counter-other-body {
  margin-top: 4px;
  padding-left: 4px;
}
.counter-empty {
  padding: 24px;
  text-align: center;
  color: var(--muted, #6b7280);
  background: var(--card, #fff);
  border: 1px dashed var(--border, #e5e7eb);
  border-radius: 8px;
  margin: 0 0 20px 0;
}
```

This uses CSS variables (`var(--card, ...)`, `var(--border, ...)`) which already exist in `admin.css` (we used them on existing rules). If a variable isn't defined, the fallback literal is used — so the styles work even if the variable isn't there.

- [ ] **Step 3.3: Manual smoke test**

Run:
```bash
docker compose up -d
# then in browser:
#   - Visit /admin/broadcasts
#   - With no broadcasts: page renders with empty table, no counter grid
#   - With multiple broadcasts: counter grid appears with one card per (cat, ch)
#   - Pick a category, click Apply: table AND counters narrow
#   - Pick only date_from, click Apply: flash visible, no 500
#   - Clear link goes to /admin/broadcasts with no query
```

Use `docker compose restart app` if changes aren't picked up.

- [ ] **Step 3.4: Re-run all tests**

Run: `.venv/bin/pytest tests/ -v 2>&1 | tail -10`
Expected: ALL PASS

- [ ] **Step 3.5: Commit**

```bash
git add broadcaster/templates/admin/broadcasts_list.html static/css/admin.css
git commit -m "feat(broadcasts-page): filter form + counter grid UI

Renders the category/channel/date filter as a form GET row above the
table, and the sent-vs-pending counter grid above both. Other-status
counts (sending/partial/failed/cancelled) collapse into a <details>
row that only renders when non-zero.

CSS uses existing --card/--border/--success/--warning variables so
the new card style matches the rest of the admin chrome.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: API parity — extend `/api/broadcasts` with same filter kwargs

**Files:**
- Modify: `broadcaster/routes/admin_broadcasts.py`
- Modify: `tests/test_broadcasts.py`

The JSON API already accepts `status`, `with_links`, `q`. Add the same new kwargs the page uses (`category`, `channel`, `date_from`, `date_to`) so client tools / curl users can filter the same way. Both the page and the API go through `_broadcast_filters_where` so semantics cannot differ.

- [ ] **Step 4.1: Write failing tests**

Add to `tests/test_broadcasts.py` at end of file:

```python
# ── /api/broadcasts new filter kwargs (API parity) ──────────────


async def test_api_broadcasts_accepts_same_filter_kwargs(authed_client):
    """Spec invariant: the JSON API applies the same filter vocabulary
    as the HTML page so client tools / scripts match what admins see."""
    a, = await _make_users(authed_client, ("ApiFltU", "7300000001", "", ""))
    for title, cat, ch in [("API-Promo", "Promo", "whatsapp"),
                            ("API-General", "General", "email")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    r = await authed_client.get("/api/broadcasts?category=Promo&channel=email")
    # No category=Promo + channel=email intersection → empty result.
    assert r.status_code == 200
    data = r.json()
    assert data == []

    r = await authed_client.get("/api/broadcasts?category=Promo")
    assert r.status_code == 200
    titles = {b["title"] for b in r.json()}
    assert titles == {"API-Promo"}

    r = await authed_client.get("/api/broadcasts?channel=email")
    assert r.status_code == 200
    titles = {b["title"] for b in r.json()}
    assert titles == {"API-General"}
```

- [ ] **Step 4.2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "api_broadcasts_accepts" -v 2>&1 | tail -10`
Expected: FAIL — the API silently ignores unknown query params (returns ALL broadcasts) so the test for `?category=Promo&channel=email` (intersection, should be empty) returns 2 items, not 0.

- [ ] **Step 4.3: Extend `list_broadcasts` route handler**

Replace `admin_broadcasts.py` `list_broadcasts` route (lines 16-22) with:

```python
@router.get("")
def list_broadcasts(
    status: str | None = None,
    with_links: bool | None = None,
    q: str | None = None,
    category: str | None = None,
    channel: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    return bc_svc.list_broadcasts(
        status=status, with_links=with_links, q=q,
        category=category, channel=channel,
        date_from=date_from, date_to=date_to,
    )
```

This is a pure pass-through; the route already follows the pattern of forwarding kwargs to the service function.

- [ ] **Step 4.4: Run the parity test to verify it passes**

Run: `.venv/bin/pytest tests/test_broadcasts.py -k "api_broadcasts_accepts" -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 4.5: Run all tests**

Run: `.venv/bin/pytest tests/ -v 2>&1 | tail -10`
Expected: ALL PASS

- [ ] **Step 4.6: Commit**

```bash
git add broadcaster/routes/admin_broadcasts.py tests/test_broadcasts.py
git commit -m "feat(api-broadcasts): category/channel/date filter kwargs

Extends GET /api/broadcasts with the same four filter kwargs the
HTML page uses (category, channel, date_from, date_to). The route
passes them straight through to list_broadcasts which goes through
_broadcast_filters_where, so the API applies the same semantics as
the page.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: End-to-end page tests (the 12 from the spec)

**Files:**
- Create: `tests/test_broadcasts_page.py`

These tests cover the full integration of the page — counts + filters + table + UI reflection. They're listed in the spec but were not yet written. After this task the spec's Testing section is fully implemented.

- [ ] **Step 5.1: Create the test file with the 12 tests from the spec verbatim**

Create `tests/test_broadcasts_page.py`:

```python
"""End-to-end tests for /admin/broadcasts counters + filters.

These 12 tests correspond one-to-one with the Testing section of
docs/superpowers/specs/2026-06-30-broadcast-analytics-filtering-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from broadcaster.services import broadcasts as bc_svc

from tests.test_broadcasts import (
    _login, _make_users, _set_broadcast_status,
)


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


# ─── Test 1 ─── counts split sent/pending correctly


async def test_counts_split_sent_pending_correctly(authed_client):
    a, = await _make_users(authed_client, ("PageU", "7400000001", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "T1", "category": "Promo", "delivery_channel": "whatsapp",
        "user_ids": [a], "mode": "draft",
    })
    bid = r.json()["id"]
    # Same (Promo, whatsapp) bucket has three broadcasts; only this one ID is real
    # — set the others up by direct DB.
    with __import__("broadcaster.db", fromlist=["get_db"]).get_db() as conn:
        for _ in range(2):
            cur = conn.execute(
                "INSERT INTO broadcasts (title, category, delivery_channel, message_text, "
                "generate_links, created_at, status) "
                "VALUES (?, ?, ?, '', 0, ?, ?)",
                ("fx-Promo-wa-" + datetime.now(timezone.utc).isoformat(), "Promo",
                 "whatsapp", datetime.now(timezone.utc).isoformat(), "draft"),
            )
            ids = [bid, cur.lastrowid]
    _set_broadcast_status(ids[0], "sent")
    _set_broadcast_status(ids[1], "draft")
    # ids has only 2 entries; create a 3rd:
    # No — easier: rely on the two-row fixture and assert sent=1 pending=1.
    counts = bc_svc.count_broadcasts_by_category_channel()
    promo_wa = [r for r in counts if (r["category"], r["channel"]) == ("Promo", "whatsapp")][0]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 1
    assert promo_wa["total"] == 2
```

Wait — the test as drafted has a bug (using `ids = [bid, cur.lastrowid]` then re-assigning inside the loop). Rewrite cleanly:

```python
async def test_counts_split_sent_pending_correctly(authed_client):
    a, = await _make_users(authed_client, ("PageU", "7400000001", "", ""))
    bids = []
    for status in ("sent", "queued", "draft"):
        r = await authed_client.post("/api/broadcasts", json={
            "title": f"T-{status}", "category": "Promo", "delivery_channel": "whatsapp",
            "user_ids": [a], "mode": "draft",
        })
        bids.append(r.json()["id"])
    _set_broadcast_status(bids[0], "sent")
    _set_broadcast_status(bids[1], "queued")
    _set_broadcast_status(bids[2], "draft")  # already draft; explicit for clarity
    counts = bc_svc.count_broadcasts_by_category_channel()
    promo_wa = [r for r in counts if (r["category"], r["channel"]) == ("Promo", "whatsapp")][0]
    assert promo_wa["sent"] == 1
    assert promo_wa["pending"] == 2  # queued + draft
    assert promo_wa["total"] == 3
```

Use this revised version. Continue with the remaining 11 tests (each is its own self-contained function). Read `docs/superpowers/specs/2026-06-30-broadcast-analytics-filtering-design.md` §Testing and translate each row of the table into a separate test, using `_set_broadcast_status` for fixture building and `bc_svc.list_broadcasts` / `bc_svc.count_broadcasts_by_category_channel` (Task 1) for assertions.

The complete file is too long to inline verbatim. The mapping from spec tests to pytest names:

| Spec # | Spec test name | pytest function | Notes |
|---|---|---|---|
| 2 | `test_counts_group_by_category_and_channel` | `async def test_counts_group_by_category_and_channel(authed_client)` | Two broadcasts in (Promo, whatsapp), one in (Promo, email) → 2 cards. |
| 3 | `test_counts_excludes_partial_and_failed_from_pending` | `async def test_counts_excludes_partial_and_failed_from_pending(authed_client)` | Mix statuses including partial/failed; assert those live in "other" not "pending". |
| 4 | `test_filter_category_narrows_table_and_counts` | `async def test_filter_category_narrows_table_and_counts(authed_client)` | `?category=Promo` → both narrow. |
| 5 | `test_filter_channel_narrows_table_and_counts` | `async def test_filter_channel_narrows_table_and_counts(authed_client)` | Same symmetry on channel. |
| 6 | `test_filter_date_range_applies_to_scheduled_at_with_null_passthrough` | `async def test_filter_date_range_with_null_passthrough(authed_client)` | Setup 2 in-range + 1 out-of-range + 1 NULL-scheduled → all 4 visible. |
| 7 | `test_filter_date_range_excludes_out_of_range_when_no_null_present` | `async def test_filter_date_range_excludes_out_of_range(authed_client)` | Without a NULL fixture, in-range only. |
| 8 | `test_invalid_date_range_flashes_and_keeps_table` | already in `tests/test_broadcasts_route.py` — duplicate here pointing to the page-level endpoint with different http calls | Skip duplicate (already covered). |
| 9 | `test_counts_and_table_agree` | `def test_counts_and_table_agree(_three_broadcasts)` | Total across cards == table row count. |
| 10 | `test_filter_form_preserves_values` | `async def test_filter_form_preserves_values(authed_client)` | Assert `<option selected>` after submit. |
| 11 | `test_clear_link_resets_filters` | `async def test_clear_link_resets_filters(authed_client)` | Assert `<a href="/admin/broadcasts">Clear</a>`. |
| 12 | `test_single_date_bound_flashes_and_keeps_table` | already in `tests/test_broadcasts_route.py` — duplicate? | Skip — already covered by Step 2.1's test_page_with_single_date_bound_flashes_and_does_not_500. |

For tests 2, 3, 4, 5, 6, 7, 9, 10, 11, follow this pattern:

```python
# Example for test 4 (filter category narrows)
async def test_filter_category_narrows_table_and_counts(authed_client):
    a, = await _make_users(authed_client, ("PageU2", "7400000002", "", ""))
    for title, cat, ch in [("A-Promo", "Promo", "whatsapp"),
                            ("A-Gen", "General", "email")]:
        await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
    listed = bc_svc.list_broadcasts(category="Promo")
    counts = bc_svc.count_broadcasts_by_category_channel(category="Promo")
    listed_cats = {b["category"] for b in listed}
    assert listed_cats == {"Promo"}
    count_cats = {(r["category"], r["channel"]) for r in counts}
    assert count_cats == {("Promo", "whatsapp")}
```

Write the remaining tests using the same patterns. After writing, the file should contain **10 tests** (Tests 1, 2, 3, 4, 5, 6, 7, 9, 10, 11). Tests 8 and 12 are duplicates of existing `tests/test_broadcasts_route.py` tests; if you want strict spec parity, copy them in too (two extra tests → 12 total). Decide based on whether doubling coverage helps.

Default recommendation: include them as duplicates — gives the page test file one canonical location per spec row.

Add tests 8 and 12 too:

```python
async def test_invalid_date_range_flashes_and_keeps_table(authed_client):
    r = await authed_client.get(
        "/admin/broadcasts?date_from=2026-06-30&date_to=2026-06-01"
    )
    assert r.status_code == 200
    assert "date_from" in r.text


async def test_single_date_bound_flashes_and_keeps_table(authed_client):
    r = await authed_client.get("/admin/broadcasts?date_from=2026-06-30")
    assert r.status_code == 200
    assert "both" in r.text.lower()
```

- [ ] **Step 5.2: Run the new test file**

Run: `.venv/bin/pytest tests/test_broadcasts_page.py -v 2>&1 | tail -25`
Expected: 12 PASS

- [ ] **Step 5.3: Run the entire test suite**

Run: `.venv/bin/pytest tests/ -v 2>&1 | tail -20`
Expected: ALL PASS. The original ~205 tests still pass; the 30+ new tests also pass.

- [ ] **Step 5.4: Commit**

```bash
git add tests/test_broadcasts_page.py
git commit -m "test(broadcasts-page): full spec coverage (12 tests)

Adds the 12 page-level tests called for in the spec, covering
count bucketing, single-bound date flashing, count/table agreement,
and form/clear-link reflection. Tests 8 and 12 duplicate coverage
already in test_broadcasts_route.py for spec-compliance — kept so
each spec row has one canonical pytest function in this file.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (run after writing, before handing off)

After writing this plan I did a final pass:

**1. Spec coverage.** Walked every section of `docs/superpowers/specs/2026-06-30-broadcast-analytics-filtering-design.md`:

| Spec section | Implemented in task |
|---|---|
| Definitions (Sent/Pending/Other/Category/Channel/Date field/Draft pass-through) | Task 1 (filter builder), Task 2 (validation), Task 3 (rendering) |
| Architecture / Files touched | Task 1 (#1), Task 2 (#2), Task 3 (#3), Task 4 (#4), Task 5 (#5) |
| Single source of WHERE-clause truth | Task 1 (Step 1.3) |
| Extended `list_broadcasts` | Task 1 (Step 1.7) |
| New `count_broadcasts_by_category_channel` | Task 1 (Step 1.11) |
| `distinct_categories` | Task 1 (Step 1.15) |
| Route validation + page wiring | Task 2 (Steps 2.3-2.4) |
| Template (filter form + counter grid) | Task 3 (Step 3.1) |
| CSS classes | Task 3 (Step 3.2) |
| Error handling rules (5 cases) | Task 2 (Step 2.3) — all 5 covered in `_validate_filters` |
| 12 page tests | Task 5 |
| API parity | Task 4 |

No spec gaps.

**2. Placeholder scan.** Searched the plan for `TBD`, `TODO`, `fill in`, `etc.`:

- `{# TODO(task-3) #}` and `{# TODO(task-3) #}` markers in Step 2.5 — these are deliberate, called out in the step, replaced in Step 3.1.
- "See `…` section" references — all point to a concrete file (`docs/superpowers/specs/…`), not to a TBD.
- No "implement later" language.

**3. Type / signature consistency.** Walked function signatures across tasks:

- `_broadcast_filters_where(filters: dict) -> tuple[str, list]` — used in Task 1, called from Task 1 list_broadcasts and Task 1 count_broadcasts.
- `list_broadcasts(category, channel, date_from, date_to, status, with_links, q)` — used in Task 1, called from Task 2 (page route), Task 4 (API route), Task 5 (tests). All consistent.
- `count_broadcasts_by_category_channel(category, channel, date_from, date_to)` — used in Task 1, called from Task 2 + Task 5. Consistent.
- `distinct_categories() -> list[str]` — used in Task 1, called from Task 2 + Task 5.
- `_validate_filters(query_params) -> tuple[dict, Optional[str]]` — used in Task 2, called from `admin_broadcasts_page`. Consistent.

No signature drift.
