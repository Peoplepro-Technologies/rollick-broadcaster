"""View tracking for the public viewer.

On the first GET of /v/{token} we:
  - set broadcast_links.first_viewed_at
  - insert one link_views row (with hashed IP + UA, never raw)

Subsequent GETs still render the page but do not re-insert a view row.
Phase 7 adds per-link rollup + analytics queries on top of this table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from broadcaster.db import get_db
from broadcaster.services import privacy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_view(
    link_id: int,
    ip: Optional[str],
    ua: Optional[str],
    referrer: Optional[str] = None,
) -> dict:
    """Mark the link as viewed. Returns {first_view, is_first}."""
    ip_hash = privacy.hash_ip(ip) if ip else None
    ua_hash = privacy.hash_ua(ua) if ua else None
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT first_viewed_at FROM broadcast_links WHERE id = ?", (link_id,)
        ).fetchone()
        is_first = row and not row["first_viewed_at"]
        if is_first:
            conn.execute(
                "UPDATE broadcast_links SET first_viewed_at = ? WHERE id = ?",
                (now, link_id),
            )
        # Always record a view row — analytics want every hit, not just first.
        conn.execute(
            "INSERT INTO link_views (link_id, viewed_at, ip_hash, ua_hash, referrer) "
            "VALUES (?, ?, ?, ?, ?)",
            (link_id, now, ip_hash, ua_hash, referrer),
        )
    return {"first_view": is_first, "viewed_at": now}
