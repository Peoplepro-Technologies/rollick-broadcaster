"""Link generation: mint per-subscriber tokens for a broadcast.

Tokens are opaque `secrets.token_urlsafe(24)` strings (~32 chars,
~192 bits). One row per (broadcast × active user), UNIQUE on both
sides. `expires_at` defaults to now + LINK_TOKEN_TTL_DAYS but a
broadcast may override per-creation.

Used by:
  - broadcasts.create_broadcast (initial generation)
  - broadcasts.update_broadcast (regenerate when targets change)
  - viewer.* (Phase 3) — resolve token to broadcast
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from broadcaster.db import get_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def mint_token() -> str:
    return secrets.token_urlsafe(24)


def generate_links_for_broadcast(
    broadcast_id: int,
    user_ids: Iterable[int],
    ttl_days: Optional[int] = None,
) -> dict:
    """Insert one `broadcast_links` row per user_id. Idempotent: existing
    (broadcast_id, user_id) rows are left alone.

    Returns {created, skipped_existing, total}.
    """
    user_ids = list(set(user_ids))
    if not user_ids:
        return {"created": 0, "skipped_existing": 0, "total": 0}

    now = _now()
    expires = (now + timedelta(days=ttl_days)) if ttl_days else None
    expires_str = _iso(expires) if expires else None
    now_str = _iso(now)

    created = 0
    skipped = 0
    with get_db() as conn:
        for uid in user_ids:
            # INSERT OR IGNORE keeps existing (broadcast_id, user_id) intact.
            cur = conn.execute(
                "INSERT OR IGNORE INTO broadcast_links "
                "(broadcast_id, user_id, token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (broadcast_id, uid, mint_token(), now_str, expires_str),
            )
            if cur.rowcount == 1:
                created += 1
            else:
                skipped += 1
    return {"created": created, "skipped_existing": skipped, "total": len(user_ids)}


def revoke_link(link_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE broadcast_links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_iso(_now()), link_id),
        )
    return cur.rowcount > 0


def resolve_token(token: str) -> Optional[dict]:
    """Phase 3 helper. Return the link row if valid (not revoked, not expired)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT bl.*, b.title, b.category, b.content_id, b.message_text, "
            "b.delivery_channel, b.status AS broadcast_status "
            "FROM broadcast_links bl "
            "JOIN broadcasts b ON b.id = bl.broadcast_id "
            "WHERE bl.token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("revoked_at"):
        return None
    if d.get("expires_at"):
        try:
            exp = datetime.fromisoformat(d["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < _now():
                return None
        except Exception:
            return None
    return d
