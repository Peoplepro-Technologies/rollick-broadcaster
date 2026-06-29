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


# ── dashboard_overview ──────────────────────────────────────


def test_dashboard_overview_empty_db():
    """Fresh DB returns zeros + empty lists, no crash."""
    from broadcaster.services.dashboard import dashboard_overview
    out = dashboard_overview()
    assert out["kpis"]["users_total"] == 0
    assert out["kpis"]["users_active"] == 0
    assert out["kpis"]["broadcasts_total"] == 0
    assert out["kpis"]["views_week"] == 0
    assert out["kpis"]["comments_week"] == 0
    assert out["kpis"]["pending_mod"] == 0
    assert out["recent_broadcasts"] == []
    assert out["pending_comments"] == []
    assert len(out["views_by_day"]) == 14


def test_dashboard_overview_kpis_reflect_seeded_data():
    """3 users, 1 broadcast, 2 views, 1 comment → counts match."""
    from broadcaster.services.dashboard import dashboard_overview
    users = [users_svc.create_user(name=f"U{i}", phone=f"71000000{i:02d}")
             for i in range(3)]
    b = bc_svc.create_broadcast(
        title="Hello", user_ids=[u["id"] for u in users])
    with get_db() as conn:
        links = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchall()
        for i, ln in enumerate(links[:2]):
            conn.execute(
                "INSERT INTO link_views (link_id, viewed_at, ip_hash, ua_hash) "
                "VALUES (?, datetime('now'), ?, ?)",
                (ln["id"], f"hash{i}", f"ua{i}"))
        first_link = links[0]["id"]
        conn.execute(
            "INSERT INTO comments (link_id, broadcast_id, body, ip_hash, status, created_at) "
            "VALUES (?, ?, 'hello', 'iphash', 'visible', datetime('now'))",
            (first_link, b["id"]))
        conn.commit()

    out = dashboard_overview()
    k = out["kpis"]
    assert k["users_total"] == 3
    assert k["users_active"] == 3
    assert k["broadcasts_total"] == 1
    assert k["views_week"] == 2
    assert k["comments_week"] == 1
    assert k["pending_mod"] == 1
    assert len(out["recent_broadcasts"]) == 1
    assert out["recent_broadcasts"][0]["title"] == "Hello"
    assert out["recent_broadcasts"][0]["link_count"] == 3
    assert out["recent_broadcasts"][0]["view_count"] == 2
    assert len(out["pending_comments"]) == 1
    assert out["pending_comments"][0]["body"] == "hello"


def test_dashboard_overview_excludes_hidden_comments_from_week_and_queue():
    """Hidden comments must NOT count toward comments_week or pending_mod."""
    from broadcaster.services.dashboard import dashboard_overview
    u = users_svc.create_user(name="U", phone="7100000099")
    b = bc_svc.create_broadcast(title="T", user_ids=[u["id"]])
    with get_db() as conn:
        link_id = conn.execute(
            "SELECT id FROM broadcast_links WHERE broadcast_id = ?",
            (b["id"],)).fetchone()["id"]
        for body, status in [("vis", "visible"), ("hid", "hidden")]:
            conn.execute(
                "INSERT INTO comments (link_id, broadcast_id, body, ip_hash, status, created_at) "
                "VALUES (?, ?, ?, 'iphash', ?, datetime('now'))",
                (link_id, b["id"], body, status))
        conn.commit()

    out = dashboard_overview()
    assert out["kpis"]["comments_week"] == 1
    assert out["kpis"]["pending_mod"] == 1
    assert len(out["pending_comments"]) == 1
    assert out["pending_comments"][0]["body"] == "vis"