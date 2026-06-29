"""Aggregations for the /admin/ dashboard.

One query batch per page load. No caching — the dashboard is hit ~once per
page-load by one admin and the queries are cheap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from broadcaster.db import get_db


def dashboard_overview() -> dict[str, Any]:
    """Return all data the dashboard template needs."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    with get_db() as conn:
        users_total = _scalar(conn, "SELECT COUNT(*) FROM users")
        users_active = _scalar(
            conn, "SELECT COUNT(*) FROM users WHERE is_active = 1")
        users_new_week = _scalar(
            conn, "SELECT COUNT(*) FROM users WHERE created_at >= ?",
            (seven_days_ago,))
        broadcasts_total = _scalar(
            conn, "SELECT COUNT(*) FROM broadcasts")
        views_week = _scalar(
            conn, "SELECT COUNT(*) FROM link_views WHERE viewed_at >= ?",
            (seven_days_ago,))
        comments_week = _scalar(
            conn,
            "SELECT COUNT(*) FROM comments "
            "WHERE created_at >= ? AND status = 'visible'",
            (seven_days_ago,))
        pending_mod = _scalar(
            conn, "SELECT COUNT(*) FROM comments WHERE status = 'visible'")

        views_by_day = _views_by_day(conn, fourteen_days_ago)

        recent_broadcasts = conn.execute("""
            SELECT b.id, b.title, b.category, b.status, b.sent_at,
                   b.created_at, b.delivery_channel,
                   (SELECT COUNT(*) FROM broadcast_links bl
                    WHERE bl.broadcast_id = b.id) AS link_count,
                   (SELECT COUNT(*) FROM link_views lv
                    JOIN broadcast_links bl ON bl.id = lv.link_id
                    WHERE bl.broadcast_id = b.id) AS view_count
            FROM broadcasts b
            ORDER BY COALESCE(b.sent_at, b.created_at) DESC
            LIMIT 5
        """).fetchall()

        pending_comments = conn.execute("""
            SELECT c.id, c.body, c.author_hint, c.created_at,
                   b.title AS broadcast_title
            FROM comments c
            JOIN broadcasts b ON b.id = c.broadcast_id
            WHERE c.status = 'visible'
            ORDER BY c.created_at ASC
            LIMIT 5
        """).fetchall()

    return {
        "kpis": {
            "users_total": users_total,
            "users_active": users_active,
            "users_new_week": users_new_week,
            "broadcasts_total": broadcasts_total,
            "views_week": views_week,
            "comments_week": comments_week,
            "pending_mod": pending_mod,
        },
        "views_by_day": views_by_day,
        "recent_broadcasts": [dict(r) for r in recent_broadcasts],
        "pending_comments": [dict(r) for r in pending_comments],
    }


def _scalar(conn, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _views_by_day(conn, since_iso: str) -> list[dict]:
    """Returns 14 entries: [{"date": "2026-06-15", "views": 42}, ...].
    Days with no views are filled with 0, so the chart's x-axis is contiguous."""
    rows = conn.execute("""
        SELECT substr(viewed_at, 1, 10) AS day, COUNT(*) AS n
        FROM link_views
        WHERE viewed_at >= ?
        GROUP BY day
        ORDER BY day
    """, (since_iso,)).fetchall()
    by_day = {r["day"]: r["n"] for r in rows}

    out = []
    start = datetime.fromisoformat(since_iso).date()
    for i in range(14):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"date": d, "views": by_day.get(d, 0)})
    return out