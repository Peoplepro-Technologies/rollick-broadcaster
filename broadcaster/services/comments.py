"""Comments service — write + moderation.

Comments are auto-published in v1 (no approval queue). Admin can hide
via Phase 6 moderation endpoints.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from broadcaster.db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _author_hint() -> str:
    """Display-only hint like 'AB#1234' — never tied to identity."""
    return f"##{secrets.token_hex(2).upper()}"


def create_comment(
    link_id: int,
    broadcast_id: int,
    body: str,
    ip_hash: str,
) -> dict:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO comments "
            "(link_id, broadcast_id, body, author_hint, ip_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'visible', ?)",
            (link_id, broadcast_id, body, _author_hint(), ip_hash, _now()),
        )
    return get_comment(cur.lastrowid)  # type: ignore[return-value]


def get_comment(cid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute(
            "SELECT id, link_id, broadcast_id, body, author_hint, status, created_at "
            "FROM comments WHERE id = ?",
            (cid,),
        ).fetchone()
    return dict(r) if r else None


def list_for_broadcast(bid: int, status: Optional[str] = "visible", q: Optional[str] = None) -> list[dict]:
    where = ["c.broadcast_id = ?"]
    params: list = [bid]
    if status:
        where.append("c.status = ?")
        params.append(status)
    if q:
        where.append("c.body LIKE ?")
        params.append(f"%{q}%")
    sql = (
        "SELECT c.id, c.link_id, c.broadcast_id, c.body, c.author_hint, c.status, c.created_at, "
        "u.name AS user_name, u.phone AS user_phone "
        "FROM comments c JOIN broadcast_links bl ON bl.id = c.link_id "
        "JOIN users u ON u.id = bl.user_id "
        "WHERE " + " AND ".join(where) + " ORDER BY c.created_at DESC"
    )
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def hide(cid: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("UPDATE comments SET status='hidden' WHERE id = ?", (cid,))
    return cur.rowcount > 0


def unhide(cid: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("UPDATE comments SET status='visible' WHERE id = ?", (cid,))
    return cur.rowcount > 0


def delete(cid: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM comments WHERE id = ?", (cid,))
    return cur.rowcount > 0


def flag(cid: int) -> bool:
    """Mark for review. v1 doesn't enforce a queue, so this is a no-op
    visible to admin via a pill on the moderation page."""
    return get_comment(cid) is not None


def list_all(broadcast_id: int | None = None, status: str | None = None,
             q: str | None = None) -> list[dict]:
    """Admin-side list of comments across all broadcasts (or one)."""
    where: list[str] = []
    params: list = []
    if broadcast_id is not None:
        where.append("c.broadcast_id = ?")
        params.append(broadcast_id)
    if status:
        where.append("c.status = ?")
        params.append(status)
    if q:
        where.append("c.body LIKE ?")
        params.append(f"%{q}%")
    sql = (
        "SELECT c.id, c.link_id, c.broadcast_id, c.body, c.author_hint, c.status, c.created_at, "
        "b.title AS broadcast_title, "
        "u.name AS user_name, u.phone AS user_phone "
        "FROM comments c "
        "JOIN broadcasts b ON b.id = c.broadcast_id "
        "JOIN broadcast_links bl ON bl.id = c.link_id "
        "JOIN users u ON u.id = bl.user_id "
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.created_at DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
