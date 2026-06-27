"""Phase 0 health check.

Verifies the scaffold boots, the DB initializes, and the public health
endpoint responds. This is the minimum bar for any phase that follows.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_returns_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "Rollick Broadcaster"
    assert "version" in body


async def test_root_index_returns_metadata(client):
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["admin"] == "/admin/login"
    assert body["health"] == "/api/health"


async def test_admin_login_page_renders(client):
    r = await client.get("/admin/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert "Rollick Broadcaster" in r.text


async def test_db_initialized_with_all_tables(app):
    """On startup, init_db should create every table in the schema."""
    from broadcaster.db import get_db

    expected = {
        "users", "groups", "group_memberships", "content",
        "broadcasts", "broadcast_targets", "broadcast_links",
        "link_views", "comments", "settings",
    }
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        actual = {row["name"] for row in rows}
    assert expected.issubset(actual), f"missing: {expected - actual}"
