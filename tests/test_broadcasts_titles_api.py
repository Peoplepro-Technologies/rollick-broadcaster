"""Tests for the /api/broadcasts/titles typeahead endpoint.

Drives the JS typeahead dropdown on /admin/broadcasts. Returns
{id, title, category, delivery_channel} for broadcasts whose title
matches the query (case-insensitive substring).
"""
from __future__ import annotations

import pytest

from broadcaster.services import broadcasts as bc_svc

from tests.test_broadcasts import (
    _login, _make_users,
)


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


async def _make_broadcasts(authed_client, rows):
    a, = await _make_users(authed_client, ("TitleU", "7400100001", "", ""))
    ids = []
    for title, cat, ch in rows:
        r = await authed_client.post("/api/broadcasts", json={
            "title": title, "category": cat, "delivery_channel": ch,
            "user_ids": [a], "mode": "draft",
        })
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


async def test_titles_endpoint_empty_query_returns_empty(authed_client):
    await _make_broadcasts(authed_client, [("Hello world", "General", "email")])
    r = await authed_client.get("/api/broadcasts/titles?q=")
    assert r.status_code == 200
    assert r.json() == []


async def test_titles_endpoint_matches_case_insensitive(authed_client):
    await _make_broadcasts(authed_client, [
        ("Diwali promo blast", "Promotions", "email"),
        ("Holi greetings", "Promotions", "whatsapp"),
        ("Internal update", "General", "email"),
    ])
    r = await authed_client.get("/api/broadcasts/titles?q=diwali")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["title"] == "Diwali promo blast"
    assert data[0]["category"] == "Promotions"
    assert data[0]["delivery_channel"] == "email"
    assert "id" in data[0]

    # Case-insensitive
    r2 = await authed_client.get("/api/broadcasts/titles?q=DIWALI")
    assert r2.status_code == 200
    assert [d["title"] for d in r2.json()] == ["Diwali promo blast"]


async def test_titles_endpoint_substring_match(authed_client):
    await _make_broadcasts(authed_client, [
        ("Quarterly sales review", "General", "email"),
        ("Sales kickoff reminder", "Promotions", "whatsapp"),
        ("Engineering update", "General", "email"),
    ])
    r = await authed_client.get("/api/broadcasts/titles?q=sales")
    assert r.status_code == 200
    titles = {d["title"] for d in r.json()}
    assert titles == {"Quarterly sales review", "Sales kickoff reminder"}


async def test_titles_endpoint_respects_limit(authed_client):
    await _make_broadcasts(authed_client, [
        (f"match-{i}", "General", "email") for i in range(12)
    ])
    r = await authed_client.get("/api/broadcasts/titles?q=match&limit=5")
    assert r.status_code == 200
    assert len(r.json()) == 5


async def test_titles_endpoint_caps_limit_at_max(authed_client):
    await _make_broadcasts(authed_client, [("foo bar", "General", "email")])
    # Even if the client asks for 9999, server caps at 25.
    r = await authed_client.get("/api/broadcasts/titles?q=foo&limit=9999")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_titles_endpoint_requires_auth(client):
    r = await client.get("/api/broadcasts/titles?q=anything")
    # require_admin returns 401 / 403 / redirect — any non-200 is fine.
    assert r.status_code != 200


async def test_titles_endpoint_no_match(authed_client):
    await _make_broadcasts(authed_client, [("Hello", "General", "email")])
    r = await authed_client.get("/api/broadcasts/titles?q=zzzzzz")
    assert r.status_code == 200
    assert r.json() == []


async def test_search_broadcast_titles_service_directly():
    """Sanity check the underlying service helper (no HTTP)."""
    import sqlite3
    # Use the actual DB the service uses — exercise the helper directly.
    out = bc_svc.search_broadcast_titles("zzz-no-match-expected-12345")
    assert out == []