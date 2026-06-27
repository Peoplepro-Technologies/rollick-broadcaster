"""Analytics queries for broadcasts + per-link rollup."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from broadcaster.db import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def broadcast_analytics(bid: int) -> dict:
    """Top-level rollup: link count, sent/viewed/comment counts, unique
    viewers, time-bucketed views, top referrers.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM broadcast_links WHERE broadcast_id = ?) AS link_count, "
            "  (SELECT COUNT(*) FROM broadcast_links WHERE broadcast_id = ? AND revoked_at IS NOT NULL) AS revoked_count, "
            "  (SELECT COUNT(*) FROM broadcast_links WHERE broadcast_id = ? AND first_viewed_at IS NOT NULL) AS viewed_count, "
            "  (SELECT COUNT(*) FROM link_views lv JOIN broadcast_links bl ON bl.id = lv.link_id WHERE bl.broadcast_id = ?) AS total_views, "
            "  (SELECT COUNT(DISTINCT lv.ip_hash) FROM link_views lv JOIN broadcast_links bl ON bl.id = lv.link_id WHERE bl.broadcast_id = ? AND lv.ip_hash IS NOT NULL) AS unique_ips, "
            "  (SELECT COUNT(*) FROM comments WHERE broadcast_id = ? AND status='visible') AS comment_count, "
            "  (SELECT COUNT(*) FROM comments WHERE broadcast_id = ? AND status='hidden') AS hidden_count",
            (bid,) * 7,
        ).fetchone()
        totals = dict(row)

        cutoff = (_now() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        bucket_rows = conn.execute(
            "SELECT substr(lv.viewed_at, 1, 10) AS day, COUNT(*) AS n "
            "FROM link_views lv JOIN broadcast_links bl ON bl.id = lv.link_id "
            "WHERE bl.broadcast_id = ? AND lv.viewed_at >= ? "
            "GROUP BY day ORDER BY day",
            (bid, cutoff_iso),
        ).fetchall()
        by_day = [dict(r) for r in bucket_rows]

        ref_rows = conn.execute(
            "SELECT COALESCE(lv.referrer, '(direct)') AS referrer, COUNT(*) AS n "
            "FROM link_views lv JOIN broadcast_links bl ON bl.id = lv.link_id "
            "WHERE bl.broadcast_id = ? AND lv.viewed_at >= ? "
            "GROUP BY referrer ORDER BY n DESC LIMIT 10",
            (bid, cutoff_iso),
        ).fetchall()
        top_referrers = [dict(r) for r in ref_rows]

    return {
        "broadcast_id": bid,
        "totals": totals,
        "views_by_day": by_day,
        "top_referrers": top_referrers,
    }


def raw_views_csv(bid: int) -> bytes:
    """Stream of all view rows for a broadcast."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT lv.viewed_at, lv.ip_hash, lv.ua_hash, lv.referrer, "
            "bl.token, u.name AS user_name, u.phone AS user_phone "
            "FROM link_views lv "
            "JOIN broadcast_links bl ON bl.id = lv.link_id "
            "JOIN users u ON u.id = bl.user_id "
            "WHERE bl.broadcast_id = ? "
            "ORDER BY lv.viewed_at",
            (bid,),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["viewed_at", "user_name", "user_phone", "token", "ip_hash", "ua_hash", "referrer"])
    for r in rows:
        d = dict(r)
        w.writerow([d["viewed_at"], d["user_name"], d["user_phone"], d["token"],
                    d["ip_hash"], d["ua_hash"], d.get("referrer") or ""])
    return buf.getvalue().encode("utf-8")
