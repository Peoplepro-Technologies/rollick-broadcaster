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
    eng = next(g for g in groups if g["name"] == "Dept: Eng")

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
    eng = next(g for g in groups if g["name"] == "Dept: Eng")

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
