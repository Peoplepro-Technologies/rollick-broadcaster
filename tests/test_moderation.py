"""Phase 6 — comment moderation."""
from __future__ import annotations

import time

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


_PHONE_COUNTER = 0


async def _make_broadcast_with_comments(client, *, n=3, body_prefix="C"):
    """Create 1 broadcast, n subscribers, n comments via public POST."""
    global _PHONE_COUNTER
    user_ids = []
    for i in range(n):
        _PHONE_COUNTER += 1
        u = (await client.post("/api/users", json={
            "name": f"U{_PHONE_COUNTER}",
            "phone": f"9{_PHONE_COUNTER:09d}",
        })).json()
        user_ids.append(u["id"])
    b = (await client.post("/api/broadcasts", json={
        "title": "Mod test", "user_ids": user_ids,
    })).json()
    bid = b["id"]
    links = (await client.get(f"/api/broadcasts/{bid}/links")).json()
    cids = []
    for i, link in enumerate(links):
        r = await client.post(f"/v/{link['token']}/comments", data={
            "body": f"{body_prefix} {i}",
            "ts_issued": str(int((time.time() - 5) * 1000)),
        })
        assert r.status_code == 200, r.text
        cids.append(r.json()["id"])
    return bid, cids


# ── List ─────────────────────────────────────────────────────

async def test_list_visible(authed_client):
    bid, _ = await _make_broadcast_with_comments(authed_client, n=2)
    r = await authed_client.get("/api/comments", params={"status": "visible"})
    body = r.json()
    assert len(body) == 2
    assert all(c["status"] == "visible" for c in body)


async def test_list_filter_by_broadcast(authed_client):
    bid1, _ = await _make_broadcast_with_comments(authed_client, n=1, body_prefix="A")
    bid2, _ = await _make_broadcast_with_comments(authed_client, n=1, body_prefix="B")
    r = await authed_client.get("/api/comments", params={"broadcast_id": bid1})
    body = r.json()
    assert all(c["broadcast_id"] == bid1 for c in body)
    assert any(c["body"].startswith("A") for c in body)


async def test_list_search_by_body(authed_client):
    bid, _ = await _make_broadcast_with_comments(authed_client, n=2, body_prefix="hello")
    bid2, _ = await _make_broadcast_with_comments(authed_client, n=2, body_prefix="world")
    r = await authed_client.get("/api/comments", params={"q": "hello"})
    body = r.json()
    assert all("hello" in c["body"] for c in body)


# ── Hide / unhide ────────────────────────────────────────────

async def test_hide_comment(authed_client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1)
    r = await authed_client.patch(f"/api/comments/{cids[0]}", json={"status": "hidden"})
    assert r.status_code == 200
    assert r.json()["status"] == "hidden"
    # Now filtered out of the visible list
    visible = (await authed_client.get("/api/comments", params={"status": "visible"})).json()
    assert all(c["id"] != cids[0] for c in visible)
    # And shown in hidden list
    hidden = (await authed_client.get("/api/comments", params={"status": "hidden"})).json()
    assert any(c["id"] == cids[0] for c in hidden)


async def test_unhide_comment(authed_client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1)
    await authed_client.patch(f"/api/comments/{cids[0]}", json={"status": "hidden"})
    r = await authed_client.patch(f"/api/comments/{cids[0]}", json={"status": "visible"})
    assert r.status_code == 200
    assert r.json()["status"] == "visible"


async def test_patch_rejects_invalid_status(authed_client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1)
    r = await authed_client.patch(f"/api/comments/{cids[0]}", json={"status": "deleted"})
    assert r.status_code == 400


async def test_patch_404(authed_client):
    r = await authed_client.patch("/api/comments/99999", json={"status": "hidden"})
    assert r.status_code == 404


# ── Delete ───────────────────────────────────────────────────

async def test_delete_comment(authed_client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1)
    r = await authed_client.delete(f"/api/comments/{cids[0]}")
    assert r.status_code == 200
    visible = (await authed_client.get("/api/comments", params={"status": "visible"})).json()
    assert all(c["id"] != cids[0] for c in visible)


async def test_delete_404(authed_client):
    r = await authed_client.delete("/api/comments/99999")
    assert r.status_code == 404


# ── Flag ─────────────────────────────────────────────────────

async def test_flag_returns_ok(authed_client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1)
    r = await authed_client.post(f"/api/comments/{cids[0]}/flag")
    assert r.status_code == 200


# ── Viewer side: hidden comments disappear from public view ──

async def test_hidden_comment_disappears_from_viewer(authed_client, client):
    bid, cids = await _make_broadcast_with_comments(authed_client, n=1, body_prefix="secrets")
    # Find the link
    from broadcaster.db import get_db
    with get_db() as conn:
        link = conn.execute("SELECT token FROM broadcast_links WHERE broadcast_id = ?", (bid,)).fetchone()
    token = link["token"]
    # Before hide: comment is visible
    r1 = await client.get(f"/v/{token}")
    assert "secrets" in r1.text
    # Hide it
    await authed_client.patch(f"/api/comments/{cids[0]}", json={"status": "hidden"})
    r2 = await client.get(f"/v/{token}")
    assert "secrets" not in r2.text


# ── Auth ─────────────────────────────────────────────────────

async def test_moderation_requires_auth(client):
    r = await client.get("/api/comments")
    assert r.status_code == 401
