"""Phase 1c — Groups CRUD + auto-group rebuild + recipient resolution."""
from __future__ import annotations

import pytest


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


# ── Create / list / delete (manual) ──────────────────────────

async def test_create_manual_group(authed_client):
    r = await authed_client.post(
        "/api/groups", json={"name": "VIPs", "type": "manual", "criteria": "{}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "VIPs"
    assert body["is_auto"] is False


async def test_create_group_requires_name(authed_client):
    r = await authed_client.post("/api/groups", json={"type": "manual"})
    assert r.status_code == 400


async def test_list_groups_empty(authed_client):
    r = await authed_client.get("/api/groups")
    assert r.status_code == 200
    assert r.json() == []


async def test_delete_manual_group(authed_client):
    cr = await authed_client.post("/api/groups", json={"name": "X"})
    gid = cr.json()["id"]
    r = await authed_client.delete(f"/api/groups/{gid}")
    assert r.status_code == 200
    r2 = await authed_client.get(f"/api/groups/{gid}")
    assert r2.status_code == 404


# ── Auto-group rebuild ───────────────────────────────────────

async def test_rebuild_creates_dept_loc_and_combo(authed_client):
    await _make_users(authed_client, *[
        ("A", "1000000001", "Eng", "BLR"),
        ("B", "1000000002", "Eng", "MUM"),
        ("C", "1000000003", "Sales", "BLR"),
        ("D", "1000000004", "Eng", "BLR"),
    ])
    r = await authed_client.post("/api/groups/rebuild-auto")
    assert r.status_code == 200
    body = r.json()
    # 2 depts (Eng, Sales) + 2 locs (BLR, MUM) + 3 combos (Eng/BLR, Eng/MUM, Sales/BLR) = 7
    assert body["created"] == 7
    assert body["departments"] == 2
    assert body["locations"] == 2
    assert body["combos"] == 3


async def test_auto_group_member_count(authed_client):
    await _make_users(authed_client, *[
        ("A", "2000000001", "Eng", "BLR"),
        ("B", "2000000002", "Eng", "BLR"),
        ("C", "2000000003", "Eng", "MUM"),
        ("D", "2000000004", "Sales", "BLR"),
    ])
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    by_name = {g["name"]: g for g in groups}
    assert by_name["Dept: Eng"]["member_count"] == 3
    assert by_name["Dept: Sales"]["member_count"] == 1
    assert by_name["Loc: BLR"]["member_count"] == 3
    assert by_name["Loc: MUM"]["member_count"] == 1
    assert by_name["Eng / BLR"]["member_count"] == 2
    assert by_name["Eng / MUM"]["member_count"] == 1
    assert by_name["Sales / BLR"]["member_count"] == 1


async def test_cannot_delete_auto_group(authed_client):
    await _make_users(authed_client, ("A", "3000000001", "Eng", "BLR"))
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    auto = next(g for g in groups if g["is_auto"])
    r = await authed_client.delete(f"/api/groups/{auto['id']}")
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot_delete_auto_group"


async def test_rebuild_is_destructive(authed_client):
    await _make_users(authed_client, ("A", "4000000001", "Eng", "BLR"))
    await authed_client.post("/api/groups/rebuild-auto")
    r1 = await authed_client.post("/api/groups/rebuild-auto")
    assert r1.json()["created"] == 3  # 1 dept + 1 loc + 1 combo
    groups = (await authed_client.get("/api/groups")).json()
    auto_count = sum(1 for g in groups if g["is_auto"])
    assert auto_count == 3  # not 6


# ── Manual group membership ──────────────────────────────────

async def test_manual_group_add_and_list_members(authed_client):
    a, b, c = await _make_users(authed_client, *[
        ("A", "5000000001", "", ""),
        ("B", "5000000002", "", ""),
        ("C", "5000000003", "", ""),
    ])
    g = (await authed_client.post("/api/groups", json={"name": "Three"})).json()
    await authed_client.post(f"/api/groups/{g['id']}/members", json={"user_ids": [a, b, c]})
    members = (await authed_client.get(f"/api/groups/{g['id']}/members")).json()
    assert {m["id"] for m in members} == {a, b, c}


async def test_manual_group_member_count(authed_client):
    a, _ = await _make_users(authed_client, ("A", "6000000001", "", ""), ("B", "6000000002", "", ""))
    g = (await authed_client.post("/api/groups", json={"name": "One"})).json()
    await authed_client.post(f"/api/groups/{g['id']}/members", json={"user_ids": [a]})
    groups = (await authed_client.get("/api/groups")).json()
    assert next(x for x in groups if x["id"] == g["id"])["member_count"] == 1


async def test_remove_member(authed_client):
    a, b = await _make_users(authed_client, ("A", "7000000001", "", ""), ("B", "7000000002", "", ""))
    g = (await authed_client.post("/api/groups", json={"name": "G"})).json()
    await authed_client.post(f"/api/groups/{g['id']}/members", json={"user_ids": [a, b]})
    r = await authed_client.delete(f"/api/groups/{g['id']}/members/{a}")
    assert r.status_code == 200
    members = (await authed_client.get(f"/api/groups/{g['id']}/members")).json()
    assert {m["id"] for m in members} == {b}


async def test_cannot_add_members_to_auto_group(authed_client):
    await _make_users(authed_client, ("A", "8000000001", "Eng", ""))
    await authed_client.post("/api/groups/rebuild-auto")
    g = next(x for x in (await authed_client.get("/api/groups")).json() if x["is_auto"])
    r = await authed_client.post(f"/api/groups/{g['id']}/members", json={"user_ids": [1]})
    assert r.status_code == 400
    assert r.json()["detail"] == "auto_group_membership_derived"


# ── Recipient resolution (used by Phase 2) ─────────────────

async def test_resolve_recipients_mixes_groups_and_users(authed_client):
    a, b, c = await _make_users(authed_client, *[
        ("A", "9000000001", "Eng", "BLR"),
        ("B", "9000000002", "Eng", "MUM"),
        ("C", "9000000003", "Sales", "BLR"),
    ])
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    eng = next(g for g in groups if g["name"] == "Dept: Eng")
    # Mix: add explicit user `c` (Sales) + the Eng group (which has a, b)
    ids = groups_svc = None  # type: ignore
    from broadcaster.services import groups as gsvc
    result = gsvc.resolve_recipients(group_ids=[eng["id"]], user_ids=[c])
    assert set(result) == {a, b, c}


async def test_resolve_recipients_dedupes(authed_client):
    a, b = await _make_users(authed_client, ("A", "9100000001", "Eng", "BLR"), ("B", "9100000002", "Eng", ""))
    await authed_client.post("/api/groups/rebuild-auto")
    groups = (await authed_client.get("/api/groups")).json()
    eng = next(g for g in groups if g["name"] == "Dept: Eng")
    from broadcaster.services import groups as gsvc
    # `a` is in Eng group; pass both [a] and the group → still just {a, b}
    result = gsvc.resolve_recipients(group_ids=[eng["id"]], user_ids=[a])
    assert set(result) == {a, b}


async def test_resolve_recipients_excludes_inactive(authed_client):
    a, b = await _make_users(authed_client, ("A", "9200000001", "Eng", ""), ("B", "9200000002", "Eng", ""))
    await authed_client.patch(f"/api/users/{b}", json={"is_active": False})
    from broadcaster.services import groups as gsvc
    g = (await authed_client.post("/api/groups", json={"name": "Manual"})).json()
    await authed_client.post(f"/api/groups/{g['id']}/members", json={"user_ids": [a, b]})
    result = gsvc.resolve_recipients(group_ids=[g["id"]], user_ids=[])
    assert result == [a]


# ── Auth ─────────────────────────────────────────────────────

async def test_groups_require_auth(client):
    r = await client.get("/api/groups")
    assert r.status_code == 401
