"""Page-route-level tests for /admin/broadcasts.

These tests exercise the SSR page directly. They cover the filter
validation logic implemented as `_validate_filters` in app.py.
"""
from __future__ import annotations

import pytest

from broadcaster.services import broadcasts as bc_svc

# Reuse the auth + user setup from test_broadcasts.
from tests.test_broadcasts import _login, _make_users  # noqa: F401


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
