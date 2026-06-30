"""Broadcast CRUD + target resolution + link generation.

The headline v1 feature: when a broadcast is created (or PATCHed with
new targets), one `broadcast_links` row is minted per active user in
the resolved recipient set. Each token is a URL the admin can include
in the WhatsApp/email message — the viewer (Phase 3) resolves it back
to the broadcast.

`generate_links` flag (broadcast.generate_links column) lets admins
opt out for plain email blasts that don't need per-recipient tracking.
Defaults to ON.

Status state machine for v1 (Phase 4 adds 'sending'/'sent'/'partial'/'failed'):
  draft → queued (via /schedule)  |  → cancelled (via /cancel)
  queued → draft (via /cancel)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional

from fastapi import HTTPException

from broadcaster.db import get_db
from broadcaster.services import groups as groups_svc
from broadcaster.services import links as links_svc
from broadcaster.settings import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_future_iso(scheduled_at: str) -> str:
    """Parse an ISO datetime string, ensure it's in the future (UTC).

    Returns the normalised ISO string (with timezone). Raises HTTP 400 on
    invalid or past datetimes so client and server share one definition.
    """
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="scheduled_at_invalid")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="scheduled_at_in_past")
    return dt.isoformat()


# ── Create ────────────────────────────────────────────────────

def create_broadcast(
    title: str,
    category: str = "General",
    message_text: Optional[str] = None,
    content_id: Optional[int] = None,
    delivery_channel: str = "whatsapp",
    group_ids: Optional[Iterable[int]] = None,
    user_ids: Optional[Iterable[int]] = None,
    generate_links: bool = True,
    created_by: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    mode: str = "draft",
) -> dict:
    if not title or not title.strip():
        raise HTTPException(status_code=400, detail="title_required")
    if delivery_channel not in ("whatsapp", "email", "both"):
        raise HTTPException(status_code=400, detail="invalid_delivery_channel")

    if mode not in ("draft", "schedule", "send_now"):
        raise HTTPException(status_code=400, detail="invalid_mode")
    if mode == "draft" and scheduled_at:
        raise HTTPException(status_code=400, detail="ambiguous_schedule_payload")
    if mode == "send_now" and scheduled_at:
        raise HTTPException(status_code=400, detail="ambiguous_schedule_payload")

    initial_status = "draft"
    normalised_scheduled_at: Optional[str] = None
    if scheduled_at is not None:
        normalised_scheduled_at = _validate_future_iso(scheduled_at)
        initial_status = "queued"
    if mode == "send_now":
        initial_status = "queued"
        if normalised_scheduled_at is None:
            normalised_scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()

    group_ids = list(group_ids or [])
    user_ids = list(user_ids or [])
    if not group_ids and not user_ids:
        raise HTTPException(status_code=400, detail="at_least_one_target_required")

    settings = get_settings()
    now_str = _now()

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO broadcasts (title, category, message_text, content_id, "
            "delivery_channel, generate_links, created_by, created_at, "
            "scheduled_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), category or "General", message_text, content_id,
             delivery_channel, 1 if generate_links else 0, created_by, now_str,
             normalised_scheduled_at, initial_status),
        )
        bid = cur.lastrowid
        for gid in group_ids:
            conn.execute(
                "INSERT INTO broadcast_targets (broadcast_id, group_id) VALUES (?, ?)",
                (bid, gid),
            )
        for uid in user_ids:
            conn.execute(
                "INSERT INTO broadcast_targets (broadcast_id, user_id) VALUES (?, ?)",
                (bid, uid),
            )

    link_info: dict = {"created": 0, "skipped_existing": 0, "total": 0}
    if generate_links:
        recipients = groups_svc.resolve_recipients(group_ids=group_ids, user_ids=user_ids)
        link_info = links_svc.generate_links_for_broadcast(
            broadcast_id=bid, user_ids=recipients, ttl_days=settings.link_token_ttl_days,
        )

    if initial_status == "queued" and normalised_scheduled_at is not None:
        from broadcaster.services import scheduler as sched_svc
        sched_svc.schedule_broadcast(bid, normalised_scheduled_at)

    b = get_broadcast(bid)
    b["link_info"] = link_info
    return b


# ── Filter WHERE-clause helper ────────────────────────────────────
# Single source of truth for the category / channel / date filter that
# the broadcasts page and the JSON API both apply. Both list_broadcasts
# and count_broadcasts_by_category_channel call this so their result
# sets cannot drift apart.
#
# Conventions:
#   - Empty string / None filter values are dropped (no clause emitted).
#   - date_from + date_to must BOTH be present, or the caller must
#     pre-validate and drop the partial range (see _validate_filters
#     in app.py). The helper emits the BETWEEN clause here regardless;
#     the caller is responsible for not calling it with partial dates.
#   - The date BETWEEN range uses `scheduled_at IS NULL OR ...` so
#     unscheduled drafts pass through the filter.
#   - The caller binds the resulting `where` string after "WHERE" and
#     the resulting `params` list to the placeholders it defined.


def _broadcast_filters_where(filters: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    category = (filters.get("category") or "").strip()
    if category:
        clauses.append("b.category = ?")
        params.append(category)

    channel = (filters.get("channel") or "").strip()
    if channel:
        clauses.append("b.delivery_channel = ?")
        params.append(channel)

    date_from = (filters.get("date_from") or "").strip()
    date_to = (filters.get("date_to") or "").strip()
    if date_from and date_to:
        from datetime import datetime as _dt, timezone as _tz
        # Use the same lexicographically-comparable ISO format the DB
        # stores (datetime.now(timezone.utc).isoformat(timespec="seconds")
        # gives YYYY-MM-DDTHH:MM:SS+00:00). Without a T-separator, naive
        # strings like "2026-06-30 00:00:00" sort AFTER the stored
        # "2026-06-30T00:00:00+00:00" — so a naive BETWEEN silently
        # matches nothing. Use ISO-from-date and bracket the day.
        try:
            d_from = _dt.fromisoformat(date_from).replace(tzinfo=_tz.utc)
            d_to = _dt.fromisoformat(date_to).replace(tzinfo=_tz.utc)
            bound_from = d_from.replace(hour=0, minute=0, second=0, microsecond=0)
            bound_to = d_to.replace(hour=23, minute=59, second=59, microsecond=0)
        except (TypeError, ValueError):
            bound_from = bound_to = None  # caller is responsible for not calling us with bad dates
        if bound_from and bound_to:
            clauses.append(
                "(b.scheduled_at IS NULL OR b.scheduled_at BETWEEN ? AND ?)"
            )
            params.append(bound_from.isoformat(timespec="seconds"))
            params.append(bound_to.isoformat(timespec="seconds"))

    where = " AND ".join(clauses)
    return where, params


# ── Read ──────────────────────────────────────────────────────

def list_broadcasts(status: Optional[str] = None, with_links: Optional[bool] = None,
                    q: Optional[str] = None,
                    category: Optional[str] = None,
                    channel: Optional[str] = None,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None) -> list[dict]:
    # Category / channel / date range come from the shared helper so
    # the JSON API and the HTML page apply identical filters.
    extra_where, extra_params = _broadcast_filters_where({
        "category": category, "channel": channel,
        "date_from": date_from, "date_to": date_to,
    })

    where: list[str] = []
    params: list = []
    if status:
        where.append("b.status = ?")
        params.append(status)
    if with_links is True:
        where.append("b.generate_links = 1")
    elif with_links is False:
        where.append("b.generate_links = 0")
    if q:
        where.append("(b.title LIKE ? OR b.message_text LIKE ?)")
        like = f"%{q}%"
        params += [like, like]
    if extra_where:
        where.append(extra_where)
        params += extra_params

    sql = (
        "SELECT b.id, b.title, b.category, b.delivery_channel, b.status, b.scheduled_at, "
        "b.sent_at, b.created_at, b.generate_links, "
        "(SELECT COUNT(*) FROM broadcast_links WHERE broadcast_id = b.id) AS link_count, "
        "(SELECT COUNT(*) FROM broadcast_targets WHERE broadcast_id = b.id) AS target_count "
        "FROM broadcasts b"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id DESC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_broadcasts_by_category_channel(
    category: Optional[str] = None,
    channel: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """One row per (category, delivery_channel) that has at least one
    broadcast in the filtered set. Each row carries per-status counts.

    Sums across rows always equal the row count of `list_broadcasts`
    applied with the same filters — both queries go through
    `_broadcast_filters_where` so they cannot drift apart.
    """
    extra_where, extra_params = _broadcast_filters_where({
        "category": category, "channel": channel,
        "date_from": date_from, "date_to": date_to,
    })

    sql = (
        "SELECT  b.category, b.delivery_channel AS channel, "
        "        SUM(CASE WHEN b.status = 'sent'                            THEN 1 ELSE 0 END) AS sent, "
        "        SUM(CASE WHEN b.status IN ('draft','queued')               THEN 1 ELSE 0 END) AS pending, "
        "        SUM(CASE WHEN b.status = 'sending'                         THEN 1 ELSE 0 END) AS sending, "
        "        SUM(CASE WHEN b.status = 'partial'                         THEN 1 ELSE 0 END) AS partial, "
        "        SUM(CASE WHEN b.status = 'failed'                          THEN 1 ELSE 0 END) AS failed, "
        "        SUM(CASE WHEN b.status = 'cancelled'                       THEN 1 ELSE 0 END) AS cancelled, "
        "        COUNT(*)                                                   AS total "
        "FROM    broadcasts b"
    )
    params: list = []
    if extra_where:
        sql += " WHERE " + extra_where
        params += extra_params
    sql += " GROUP BY b.category, b.delivery_channel HAVING COUNT(*) > 0 ORDER BY b.category, b.delivery_channel"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def distinct_categories() -> list[str]:
    """Distinct categories currently in the broadcasts table, sorted.

    Used to populate the filter `<select>` on the broadcasts page
    without forcing the admin to maintain a separate categories table.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM broadcasts WHERE category IS NOT NULL "
            "AND category != '' ORDER BY category"
        ).fetchall()
    return [r[0] for r in rows]


def get_broadcast(bid: int) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute(
            "SELECT * FROM broadcasts WHERE id = ?", (bid,)
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["generate_links"] = bool(d["generate_links"])
    # targets
    with get_db() as conn:
        tg = conn.execute(
            "SELECT bt.id, bt.group_id, bt.user_id, "
            "COALESCE(g.name, '') AS group_name, COALESCE(u.name, '') AS user_name "
            "FROM broadcast_targets bt "
            "LEFT JOIN groups g ON g.id = bt.group_id "
            "LEFT JOIN users u ON u.id = bt.user_id "
            "WHERE bt.broadcast_id = ?", (bid,)
        ).fetchall()
    d["targets"] = [dict(t) for t in tg]
    return d


def list_links(bid: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT bl.id, bl.broadcast_id, bl.user_id, bl.token, bl.created_at, "
            "bl.expires_at, bl.revoked_at, bl.first_viewed_at, "
            "u.name AS user_name, u.phone AS user_phone, u.email AS user_email, "
            "(SELECT COUNT(*) FROM link_views WHERE link_id = bl.id) AS view_count, "
            "(SELECT COUNT(*) FROM comments WHERE link_id = bl.id AND status='visible') AS comment_count "
            "FROM broadcast_links bl "
            "JOIN users u ON u.id = bl.user_id "
            "WHERE bl.broadcast_id = ? "
            "ORDER BY bl.id",
            (bid,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Update / state transitions ────────────────────────────────

def update_broadcast(bid: int, **fields) -> Optional[dict]:
    b = get_broadcast(bid)
    if not b:
        return None
    if b["status"] in ("sent", "sending", "partial", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"cannot_edit_{b['status']}_broadcast")

    allowed = {"title", "category", "message_text", "content_id", "delivery_channel",
               "scheduled_at", "generate_links"}
    sets, params = [], []
    targets_changed = False
    new_group_ids: Optional[list] = None
    new_user_ids: Optional[list] = None

    for k, v in fields.items():
        if k == "group_ids":
            new_group_ids = list(v or [])
            targets_changed = True
        elif k == "user_ids":
            new_user_ids = list(v or [])
            targets_changed = True
        elif k in allowed and v is not None:
            if k == "generate_links":
                v = 1 if v else 0
            sets.append(f"{k} = ?"); params.append(v)

    with get_db() as conn:
        if sets:
            params.append(bid)
            conn.execute(f"UPDATE broadcasts SET {', '.join(sets)} WHERE id = ?", params)
        if targets_changed:
            # Replace targets
            conn.execute("DELETE FROM broadcast_targets WHERE broadcast_id = ?", (bid,))
            for gid in (new_group_ids or []):
                conn.execute(
                    "INSERT INTO broadcast_targets (broadcast_id, group_id) VALUES (?, ?)",
                    (bid, gid),
                )
            for uid in (new_user_ids or []):
                conn.execute(
                    "INSERT INTO broadcast_targets (broadcast_id, user_id) VALUES (?, ?)",
                    (bid, uid),
                )

    # Re-mint links for the new recipient set — outside the prior transaction.
    if targets_changed and b.get("generate_links"):
        settings = get_settings()
        recipients = groups_svc.resolve_recipients(
            group_ids=new_group_ids or [], user_ids=new_user_ids or [],
        )
        links_svc.generate_links_for_broadcast(
            broadcast_id=bid, user_ids=recipients, ttl_days=settings.link_token_ttl_days,
        )

    return get_broadcast(bid)


def schedule_broadcast(bid: int, when_iso: str) -> dict:
    """Set status=queued + scheduled_at and register with the scheduler."""
    with get_db() as conn:
        b = conn.execute("SELECT status FROM broadcasts WHERE id = ?", (bid,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="not_found")
        if b["status"] not in ("draft", "queued"):
            raise HTTPException(status_code=400, detail=f"cannot_schedule_{b['status']}_broadcast")
        conn.execute(
            "UPDATE broadcasts SET scheduled_at = ?, status = 'queued' WHERE id = ?",
            (when_iso, bid),
        )
    # Register with the scheduler (or fire immediately if overdue)
    from broadcaster.services import scheduler as sched_svc
    sched_svc.schedule_broadcast(bid, when_iso)
    return get_broadcast(bid)  # type: ignore[return-value]


def cancel_broadcast(bid: int) -> dict:
    with get_db() as conn:
        b = conn.execute("SELECT status FROM broadcasts WHERE id = ?", (bid,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="not_found")
        if b["status"] in ("sent", "sending", "partial", "failed", "cancelled"):
            raise HTTPException(status_code=400, detail=f"cannot_cancel_{b['status']}_broadcast")
        conn.execute("UPDATE broadcasts SET status = 'cancelled' WHERE id = ?", (bid,))
    from broadcaster.services import scheduler as sched_svc
    sched_svc.cancel_broadcast(bid)
    return get_broadcast(bid)  # type: ignore[return-value]


def delete_broadcast(bid: int) -> bool:
    with get_db() as conn:
        b = conn.execute("SELECT status FROM broadcasts WHERE id = ?", (bid,)).fetchone()
        if not b:
            return False
        # FK cascade on broadcast_links, link_views, comments
        conn.execute("DELETE FROM broadcasts WHERE id = ?", (bid,))
    return True


# ── Send fan-out ─────────────────────────────────────────────

def send_broadcast(bid: int) -> dict:
    """Iterate the broadcast's active links, build per-user messages,
    push through the appropriate sender(s), and record counters.

    Status transitions:
      draft|queued → sending → sent | partial | failed
    """
    b = get_broadcast(bid)
    if not b:
        raise HTTPException(status_code=404, detail="not_found")
    if b["status"] in ("sent", "sending", "partial", "failed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"cannot_send_{b['status']}_broadcast")

    from broadcaster.services.senders import Message, channels_to_use, get_sender_for

    settings = get_settings()
    base = settings.base_public_url.rstrip("/")
    now_str = _now()

    with get_db() as conn:
        conn.execute("UPDATE broadcasts SET status = 'sending' WHERE id = ?", (bid,))
        links = conn.execute(
            "SELECT bl.id AS link_id, bl.token, bl.user_id, "
            "u.name AS user_name, u.phone AS user_phone, u.email AS user_email "
            "FROM broadcast_links bl JOIN users u ON u.id = bl.user_id "
            "WHERE bl.broadcast_id = ? AND bl.revoked_at IS NULL AND u.is_active = 1",
            (bid,),
        ).fetchall()

    channels = channels_to_use(b["delivery_channel"])
    counters: dict[str, dict[str, int]] = {ch: {"sent": 0, "failed": 0} for ch in channels}

    for link in links:
        viewer_link = f"{base}/v/{link['token']}"
        body_text = _render_message(b, viewer_link)
        for ch in channels:
            recipient = link["user_phone"] if ch == "whatsapp" else link["user_email"]
            if not recipient:
                counters[ch]["failed"] += 1
                continue
            sender = get_sender_for(ch)
            msg = Message(
                channel=ch,
                recipient=recipient,
                subject=b["title"] if ch == "email" else None,
                body=body_text,
                viewer_link=viewer_link,
                broadcast_id=bid,
                user_id=link["user_id"],
                link_id=link["link_id"],
            )
            result = sender.send(msg)
            if result.ok:
                counters[ch]["sent"] += 1
            else:
                counters[ch]["failed"] += 1

    # Finalize status
    total_sent = sum(c["sent"] for c in counters.values())
    total_failed = sum(c["failed"] for c in counters.values())
    if total_sent > 0 and total_failed == 0:
        final = "sent"
    elif total_sent > 0 and total_failed > 0:
        final = "partial"
    elif total_sent == 0 and total_failed > 0:
        final = "failed"
    else:
        final = "sent"  # no recipients — vacuously successful

    def _status_str(ch: str) -> str | None:
        if ch not in counters:
            return None
        c = counters[ch]
        return f"sent:{c['sent']},failed:{c['failed']}"

    with get_db() as conn:
        conn.execute(
            "UPDATE broadcasts SET status = ?, sent_at = ?, "
            "whatsapp_status = ?, email_status = ? WHERE id = ?",
            (final, now_str, _status_str("whatsapp"), _status_str("email"), bid),
        )

    return {
        "broadcast_id": bid,
        "status": final,
        "sent_at": now_str,
        "counters": counters,
    }


def _render_message(b: dict, viewer_link: str) -> str:
    """Compose the per-user message body. The {{viewer_link}} placeholder
    in the admin-supplied message_text is replaced with the actual URL.
    """
    body = b.get("message_text") or ""
    body = body.replace("{{viewer_link}}", viewer_link).replace("{{link}}", viewer_link)
    if viewer_link not in body:
        # Always include the link so subscribers can click.
        body = f"{body}\n\n{viewer_link}".strip()
    title = b.get("title") or ""
    if title and not body.startswith(title):
        body = f"{title}\n\n{body}"
    return body
