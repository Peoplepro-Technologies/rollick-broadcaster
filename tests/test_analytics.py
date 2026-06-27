"""Phase 7 — analytics + per-link rollup + CSV export."""
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


async def _setup_broadcast_with_views(authed_client, viewer_client, *, n_users=3, n_views=2):
    """Create broadcast with N users, then have M views by hitting /v/{token}."""
    uids = []
    for i in range(n_users):
        u = (await authed_client.post("/api/users", json={
            "name": f"A{i}", "phone": f"800{1000000 + i:07d}",
        })).json()
        uids.append(u["id"])
    b = (await authed_client.post("/api/broadcasts", json={
        "title": "Stats", "user_ids": uids,
    })).json()
    bid = b["id"]
    links = (await authed_client.get(f"/api/broadcasts/{bid}/links")).json()
    for link in links[:n_views]:
        for _ in range(2):
            await viewer_client.get(f"/v/{link['token']}")
    return bid, links


# ── Totals ───────────────────────────────────────────────────

async def test_analytics_totals(authed_client, client):
    bid, _ = await _setup_broadcast_with_views(authed_client, client, n_users=2, n_views=2)
    r = await authed_client.get(f"/api/broadcasts/{bid}/analytics")
    assert r.status_code == 200
    body = r.json()
    t = body["totals"]
    assert t["link_count"] == 2
    assert t["viewed_count"] == 2
    assert t["total_views"] == 4  # 2 links × 2 hits
    assert t["unique_ips"] >= 1
    assert t["comment_count"] == 0
    assert t["revoked_count"] == 0


async def test_analytics_counts_unique_ips(authed_client, client):
    # All views come from the test client → same IP → unique_ips=1
    bid, _ = await _setup_broadcast_with_views(authed_client, client, n_users=3, n_views=3)
    r = await authed_client.get(f"/api/broadcasts/{bid}/analytics")
    assert r.json()["totals"]["unique_ips"] == 1


async def test_analytics_counts_comments(authed_client, client):
    bid, links = await _setup_broadcast_with_views(authed_client, client, n_users=2, n_views=1)
    # Post 1 comment on the first link
    r = await client.post(f"/v/{links[0]['token']}/comments", data={
        "body": "Loved it", "ts_issued": str(int((time.time() - 5) * 1000)),
    })
    assert r.status_code == 200
    analytics = (await authed_client.get(f"/api/broadcasts/{bid}/analytics")).json()
    assert analytics["totals"]["comment_count"] == 1


async def test_analytics_counts_revoked(authed_client, client):
    bid, links = await _setup_broadcast_with_views(authed_client, client, n_users=2, n_views=1)
    await authed_client.post(f"/api/broadcasts/{bid}/links/{links[0]['id']}/revoke")
    analytics = (await authed_client.get(f"/api/broadcasts/{bid}/analytics")).json()
    assert analytics["totals"]["revoked_count"] == 1


# ── Time-bucketed views ────────────────────────────────────

async def test_views_by_day_present(authed_client, client):
    bid, _ = await _setup_broadcast_with_views(authed_client, client, n_users=1, n_views=1)
    body = (await authed_client.get(f"/api/broadcasts/{bid}/analytics")).json()
    assert isinstance(body["views_by_day"], list)
    assert len(body["views_by_day"]) >= 1
    assert "day" in body["views_by_day"][0]
    assert "n" in body["views_by_day"][0]


# ── CSV export ──────────────────────────────────────────────

async def test_views_csv_export(authed_client, client):
    bid, _ = await _setup_broadcast_with_views(authed_client, client, n_users=1, n_views=1)
    r = await authed_client.get(f"/api/broadcasts/{bid}/views.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    # Parse the CSV
    import csv, io
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["viewed_at", "user_name", "user_phone", "token", "ip_hash", "ua_hash", "referrer"]
    assert len(rows) >= 2  # at least 1 data row
    # IP should be hashed (64 chars), not raw
    data_row = rows[1]
    assert len(data_row[4]) == 64  # ip_hash


# ── Auth ─────────────────────────────────────────────────────

async def test_analytics_requires_auth(client):
    r = await client.get("/api/broadcasts/1/analytics")
    assert r.status_code == 401
    r = await client.get("/api/broadcasts/1/views.csv")
    assert r.status_code == 401
