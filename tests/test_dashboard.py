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