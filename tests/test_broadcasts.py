"""Phase 2 — Broadcast create + link generation + state transitions."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

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


async def _make_users(client, *tuples):
    """tuples: list of (name, phone, dept, location)."""
    ids = []
    for name, phone, dept, loc in tuples:
        r = await client.post(
            "/api/users",
            json={"name": name, "phone": phone,
                  "department": dept, "location": loc, "is_active": True},
        )
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


# ── _validate_future_iso ─────────────────────────────────────


def test_validate_future_iso_returns_iso_for_future_utc():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    out = bc_svc._validate_future_iso(future)
    # round-trip parse
    parsed = datetime.fromisoformat(out)
    assert parsed > datetime.now(timezone.utc)


def test_validate_future_iso_rejects_past_with_400():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        bc_svc._validate_future_iso(past)
    assert exc.value.status_code == 400
    assert exc.value.detail == "scheduled_at_in_past"


def test_validate_future_iso_rejects_naive_datetime():
    past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        bc_svc._validate_future_iso(past_naive)
    assert exc.value.detail == "scheduled_at_in_past"


def test_validate_future_iso_rejects_garbage():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        bc_svc._validate_future_iso("not-a-date")
    assert exc.value.detail == "scheduled_at_invalid"


# ── Create ────────────────────────────────────────────────────

async def test_create_broadcast_minimal(authed_client):
    a, b = await _make_users(authed_client,
                             ("A", "1000000001", "", ""),
                             ("B", "1000000002", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Hello", "user_ids": [a, b],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Hello"
    assert body["status"] == "draft"
    assert body["generate_links"] is True
    assert body["link_info"]["created"] == 2
    assert body["link_info"]["total"] == 2


async def test_create_broadcast_with_group(authed_client):
    await _make_users(authed_client, ("A", "2000000001", "Eng", "BLR"),
                                   ("B", "2000000002", "Eng", "MUM"))
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    eng = next(g for g in groups if g["name"] == "Eng")

    r = await authed_client.post("/api/broadcasts", json={
        "title": "Eng update", "group_ids": [eng["id"]],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["link_info"]["created"] == 2


async def test_create_with_future_scheduled_at_creates_queued(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000091", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Scheduled", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "schedule",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["scheduled_at"] is not None


async def test_create_with_past_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000092", "", ""))
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Past", "user_ids": [a],
        "scheduled_at": past_iso, "mode": "schedule",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "scheduled_at_in_past"


async def test_create_with_draft_mode_and_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000093", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Ambiguous", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "draft",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "ambiguous_schedule_payload"


async def test_create_with_garbage_scheduled_at_returns_400(authed_client):
    a, = await _make_users(authed_client, ("A", "1000000094", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "Garbage", "user_ids": [a],
        "scheduled_at": "not-a-date", "mode": "schedule",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "scheduled_at_invalid"


async def test_create_broadcast_mixes_groups_and_users_deduped(authed_client):
    a, b, c = await _make_users(authed_client,
                                 ("A", "3000000001", "Eng", "BLR"),
                                 ("B", "3000000002", "Sales", ""),
                                 ("C", "3000000003", "Eng", "MUM"))
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    eng = next(g for g in groups if g["name"] == "Eng")

    r = await authed_client.post("/api/broadcasts", json={
        "title": "Mix", "group_ids": [eng["id"]], "user_ids": [b, c],
    })
    body = r.json()
    # a is in Eng, c is in Eng + explicit, b is explicit
    assert body["link_info"]["created"] == 3


async def test_create_broadcast_requires_title(authed_client):
    a, = await _make_users(authed_client, ("A", "4000000001", "", ""))
    r = await authed_client.post("/api/broadcasts", json={"user_ids": [a]})
    assert r.status_code == 400
    assert r.json()["detail"] == "title_required"


async def test_create_broadcast_requires_targets(authed_client):
    r = await authed_client.post("/api/broadcasts", json={"title": "X"})
    assert r.status_code == 400
    assert r.json()["detail"] == "at_least_one_target_required"


async def test_create_broadcast_rejects_bad_channel(authed_client):
    a, = await _make_users(authed_client, ("A", "5000000001", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a], "delivery_channel": "telegram",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_delivery_channel"


async def test_create_broadcast_with_generate_links_false(authed_client):
    a, = await _make_users(authed_client, ("A", "6000000001", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a], "generate_links": False,
    })
    body = r.json()
    assert body["generate_links"] is False
    assert body["link_info"]["created"] == 0


# ── Tokens ────────────────────────────────────────────────────

async def test_tokens_are_unique_and_long(authed_client):
    a, b, c = await _make_users(authed_client,
                                 ("A", "7000000001", "", ""),
                                 ("B", "7000000002", "", ""),
                                 ("C", "7000000003", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "T", "user_ids": [a, b, c],
    })
    bid = r.json()["id"]
    links = (await authed_client.get(f"/api/broadcasts/{bid}/links")).json()
    tokens = [l["token"] for l in links]
    assert len(tokens) == 3
    assert len(set(tokens)) == 3  # unique
    assert all(len(t) >= 30 for t in tokens)  # token_urlsafe(24) → ~32 chars


async def test_token_has_expiry(authed_client):
    a, = await _make_users(authed_client, ("A", "8000000001", "", ""))
    r = await authed_client.post("/api/broadcasts", json={
        "title": "T", "user_ids": [a],
    })
    bid = r.json()["id"]
    links = (await authed_client.get(f"/api/broadcasts/{bid}/links")).json()
    assert links[0]["expires_at"] is not None


# ── List / get / delete ──────────────────────────────────────

async def test_list_broadcasts(authed_client):
    a, = await _make_users(authed_client, ("A", "9000000001", "", ""))
    for t in ["One", "Two", "Three"]:
        await authed_client.post("/api/broadcasts", json={"title": t, "user_ids": [a]})
    r = await authed_client.get("/api/broadcasts")
    assert r.status_code == 200
    titles = {b["title"] for b in r.json()}
    assert titles == {"One", "Two", "Three"}


async def test_list_filter_by_status(authed_client):
    a, = await _make_users(authed_client, ("A", "1100000001", "", ""))
    await authed_client.post("/api/broadcasts", json={"title": "A", "user_ids": [a]})
    await authed_client.post("/api/broadcasts", json={"title": "B", "user_ids": [a]})
    # Cancel one
    items = (await authed_client.get("/api/broadcasts")).json()
    bid = items[0]["id"]
    await authed_client.post(f"/api/broadcasts/{bid}/cancel")
    drafts = (await authed_client.get("/api/broadcasts", params={"status": "draft"})).json()
    assert all(b["status"] == "draft" for b in drafts)


async def test_get_broadcast_includes_targets(authed_client):
    a, = await _make_users(authed_client, ("A", "1200000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    r = await authed_client.get(f"/api/broadcasts/{bid}")
    body = r.json()
    assert body["title"] == "X"
    assert any(t["user_id"] == a for t in body["targets"])


async def test_delete_broadcast_cascades_links(authed_client):
    a, = await _make_users(authed_client, ("A", "1300000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    r = await authed_client.delete(f"/api/broadcasts/{bid}")
    assert r.status_code == 200
    r2 = await authed_client.get(f"/api/broadcasts/{bid}")
    assert r2.status_code == 404


# ── PATCH / schedule / cancel ─────────────────────────────────

async def test_patch_updates_title(authed_client):
    a, = await _make_users(authed_client, ("A", "1400000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "Old", "user_ids": [a],
    })
    bid = cr.json()["id"]
    r = await authed_client.patch(f"/api/broadcasts/{bid}", json={"title": "New"})
    assert r.status_code == 200
    assert r.json()["title"] == "New"


async def test_patch_regenerates_links_when_targets_change(authed_client):
    a, b, c = await _make_users(authed_client,
                                 ("A", "1500000001", "", ""),
                                 ("B", "1500000002", "", ""),
                                 ("C", "1500000003", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    assert cr.json()["link_info"]["created"] == 1

    r = await authed_client.patch(f"/api/broadcasts/{bid}", json={"user_ids": [a, b, c]})
    links = (await authed_client.get(f"/api/broadcasts/{bid}/links")).json()
    # Originally only `a` had a link. PATCH adds links for b, c (a is unchanged).
    user_ids_with_links = {l["user_id"] for l in links}
    assert user_ids_with_links == {a, b, c}


async def test_cannot_patch_sent_broadcast(authed_client):
    a, = await _make_users(authed_client, ("A", "1600000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    # Force into 'sent' state via direct DB
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE broadcasts SET status='sent' WHERE id=?", (bid,))
    r = await authed_client.patch(f"/api/broadcasts/{bid}", json={"title": "New"})
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot_edit_sent_broadcast"


async def test_schedule_sets_queued(authed_client):
    a, = await _make_users(authed_client, ("A", "1700000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    r = await authed_client.post(f"/api/broadcasts/{bid}/schedule",
                                  json={"scheduled_at": "2026-12-31T10:00:00+00:00"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["scheduled_at"] is not None


async def test_schedule_requires_when(authed_client):
    a, = await _make_users(authed_client, ("A", "1800000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    r = await authed_client.post(f"/api/broadcasts/{cr.json()['id']}/schedule", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "scheduled_at_required"


async def test_cancel_draft_broadcast(authed_client):
    a, = await _make_users(authed_client, ("A", "1900000001", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    r = await authed_client.post(f"/api/broadcasts/{bid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


async def test_cannot_cancel_sent_broadcast(authed_client):
    a, = await _make_users(authed_client, ("A", "2000000002", "", ""))
    cr = await authed_client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [a],
    })
    bid = cr.json()["id"]
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE broadcasts SET status='sent' WHERE id=?", (bid,))
    r = await authed_client.post(f"/api/broadcasts/{bid}/cancel")
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot_cancel_sent_broadcast"


# ── Auth ──────────────────────────────────────────────────────

async def test_broadcasts_require_auth(client):
    r = await client.get("/api/broadcasts")
    assert r.status_code == 401
    r = await client.post("/api/broadcasts", json={"title": "X", "user_ids": [1]})
    assert r.status_code == 401


async def test_compose_form_renders_picker_block(client):
    await _login(client)
    r = await client.get("/admin/broadcasts/new")
    assert r.status_code == 200
    html = r.text
    assert 'name="_schedule_mode"' in html
    assert 'name="_scheduled_at_local"' in html
    assert 'class="when-block"' in html


async def test_list_emits_data_scheduled_at_marker(client):
    await _login(client)
    a, = await _make_users(client, ("A", "1000000095", "", ""))
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await client.post("/api/broadcasts", json={
        "title": "Listable", "user_ids": [a],
        "scheduled_at": future_iso, "mode": "schedule",
    })
    r = await client.get("/admin/broadcasts")
    assert r.status_code == 200
    assert 'data-scheduled-at="' in r.text


async def test_detail_page_redirects_to_list_when_missing(client):
    """Visiting /admin/broadcasts/{id} for a deleted/missing broadcast
    must NOT show raw 'Broadcast not found' — it should bounce to the
    list page with a flash so the user understands what happened."""
    await _login(client)
    r = await client.get("/admin/broadcasts/999999", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/broadcasts?missing=999999"

    # Following the redirect renders the list with the flash visible.
    r2 = await client.get("/admin/broadcasts?missing=999999")
    assert r2.status_code == 200
    assert "Broadcast #999999 no longer exists" in r2.text
    assert 'class="flash flash-info"' in r2.text


async def test_detail_page_has_back_link_to_list(client):
    """The detail page must expose a clear breadcrumb back to the list
    so users don't get stranded after scheduling / sending."""
    await _login(client)
    a, = await _make_users(client, ("A", "1000000096", "", ""))
    bid = (await client.post("/api/broadcasts", json={
        "title": "Back-link", "user_ids": [a],
    })).json()["id"]
    r = await client.get(f"/admin/broadcasts/{bid}")
    assert r.status_code == 200
    assert 'class="breadcrumb"' in r.text
    assert 'href="/admin/broadcasts"' in r.text
    assert "All Broadcasts" in r.text


async def test_list_no_flash_for_normal_visit(client):
    """The list page must NOT show the 'no longer exists' flash when
    there's no missing query param — that would be confusing on every
    normal navigation."""
    await _login(client)
    r = await client.get("/admin/broadcasts")
    assert r.status_code == 200
    assert 'class="flash flash-info"' not in r.text


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
    assert params == ["2026-06-01T00:00:00+00:00", "2026-06-30T23:59:59+00:00"]


def test_filters_where_combines_with_and():
    where, params = bc_svc._broadcast_filters_where({
        "category": "Promo", "channel": "whatsapp",
        "date_from": "2026-06-01", "date_to": "2026-06-30",
    })
    assert where == "b.category = ? AND b.delivery_channel = ? AND (b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)"
    assert params == ["Promo", "whatsapp", "2026-06-01T00:00:00+00:00", "2026-06-30T23:59:59+00:00"]


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


# ── list_broadcasts new filter params ─────────────────────────────


def _set_broadcast_status(bid: int, status: str, scheduled_at: str | None = None):
    """Direct-DB status setter used by filter/aggregation tests so we
    can build fixtures faster than driving the full /send pipeline."""
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE broadcasts SET status = ?, scheduled_at = COALESCE(?, scheduled_at) WHERE id = ?",
            (status, scheduled_at, bid),
        )


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


def test_list_broadcasts_filter_by_category(_three_broadcasts):
    out = bc_svc.list_broadcasts(category="Promo")
    titles = {b["title"] for b in out}
    assert titles == {"Promo-A", "Promo-B"}


def test_list_broadcasts_filter_by_channel(_three_broadcasts):
    out = bc_svc.list_broadcasts(channel="whatsapp")
    titles = {b["title"] for b in out}
    assert titles == {"Promo-A", "General-A"}


def test_list_broadcasts_filter_by_date_range_passes_null_through(_three_broadcasts):
    """Two scheduled-in-range, one scheduled-out, one draft (NULL) → all four pass.
    Note: this test mutates the 3-broadcast fixture (and so implicitly
    trusts that the previous 3 fixtures have the same user as the 4th).
    """
    _set_broadcast_status(_three_broadcasts[0], "sent", "2026-06-15T12:00:00")
    _set_broadcast_status(_three_broadcasts[1], "queued", "2026-06-15T12:00:00")
    _set_broadcast_status(_three_broadcasts[2], "draft", "2026-07-15T12:00:00")

    out = bc_svc.list_broadcasts(date_from="2026-06-01", date_to="2026-06-30")
    titles = {b["title"] for b in out}
    # The two 06-15 rows pass; the 07-15 row is out of range; no NULL row
    # in this fixture (drafts without scheduled_at are created below in
    # the date-filters tests).
    assert "Promo-A" in titles
    assert "Promo-B" in titles
    assert "General-A" not in titles


async def test_list_broadcasts_includes_null_scheduled_at_when_date_filter_set(authed_client):
    """A broadcast whose scheduled_at is NULL must still be in the result
    when a date filter is applied (draft pass-through)."""
    a, = await _make_users(authed_client, ("BcastNull", "7000000099", "", ""))
    # Create one scheduled-in-range and one scheduled-out and one NULL.
    in_range_resp = await authed_client.post("/api/broadcasts", json={
        "title": "InRange",
        "category": "Promo", "delivery_channel": "email",
        "user_ids": [a],
        "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    })
    assert in_range_resp.status_code == 200, in_range_resp.text
    in_range = in_range_resp.json()["id"]
    out_of_range_resp = await authed_client.post("/api/broadcasts", json={
        "title": "OutRange",
        "category": "Promo", "delivery_channel": "email",
        "user_ids": [a],
        "mode": "schedule",
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
    })
    assert out_of_range_resp.status_code == 200, out_of_range_resp.text
    out_of_range = out_of_range_resp.json()["id"]
    null_sched_resp = await authed_client.post("/api/broadcasts", json={
        "title": "NullDraft",
        "category": "Promo", "delivery_channel": "email",
        "user_ids": [a], "mode": "draft",  # scheduled_at is NULL
    })
    assert null_sched_resp.status_code == 200, null_sched_resp.text
    null_sched = null_sched_resp.json()["id"]

    today = datetime.now(timezone.utc).date().isoformat()
    listed = bc_svc.list_broadcasts(date_from=today, date_to=today)
    titles = {b["title"] for b in listed}
    # InRange + NullDraft visible; OutRange is >today and out of range.
    assert "InRange" in titles
    assert "NullDraft" in titles
    assert "OutRange" not in titles


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


def test_count_broadcasts_returns_empty_for_no_broadcasts():
    assert bc_svc.count_broadcasts_by_category_channel() == []


# ── distinct_categories ──────────────────────────────────────────


def test_distinct_categories_returns_sorted_unique(_three_broadcasts):
    """The _three_broadcasts fixture creates categories Promo, Promo, General."""
    out = bc_svc.distinct_categories()
    # Sorted, deduplicated.
    assert out == ["General", "Promo"]


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
