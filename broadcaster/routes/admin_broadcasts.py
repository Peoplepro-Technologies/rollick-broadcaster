"""Admin broadcasts router — CRUD + link list + state transitions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from broadcaster.routes.admin_auth import require_admin
from broadcaster.services import broadcasts as bc_svc

router = APIRouter(
    prefix="/api/broadcasts",
    tags=["broadcasts"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
def list_broadcasts(
    status: str | None = None,
    with_links: bool | None = None,
    q: str | None = None,
    category: str | None = None,
    channel: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    return bc_svc.list_broadcasts(
        status=status, with_links=with_links, q=q,
        category=category, channel=channel,
        date_from=date_from, date_to=date_to,
    )


@router.get("/titles")
def title_suggestions(q: str = "", limit: int = 8):
    """Lightweight typeahead endpoint — returns id/title/category/channel
    for broadcasts whose title matches `q` (case-insensitive substring).

    Used by the search box on /admin/broadcasts to surface suggestions
    as the admin types without reloading the page.
    """
    limit = max(1, min(int(limit or 8), 25))
    return bc_svc.search_broadcast_titles(q=q, limit=limit)


@router.post("")
def create_broadcast(payload: dict, request_admin_id: int = Depends(require_admin)):
    from broadcaster.services import admin as admin_svc
    creator = admin_svc.find_by_id(request_admin_id)
    return bc_svc.create_broadcast(
        title=payload.get("title", ""),
        category=payload.get("category", "General"),
        message_text=payload.get("message_text"),
        content_id=payload.get("content_id"),
        delivery_channel=payload.get("delivery_channel") or "email",
        group_ids=payload.get("group_ids") or [],
        user_ids=payload.get("user_ids") or [],
        generate_links=bool(payload.get("generate_links", True)),
        created_by=creator["username"] if creator else None,
        scheduled_at=payload.get("scheduled_at"),
        mode=payload.get("mode", "draft"),
    )


@router.get("/{bid}")
def get_broadcast(bid: int):
    b = bc_svc.get_broadcast(bid)
    if not b:
        raise HTTPException(status_code=404, detail="not_found")
    return b


@router.patch("/{bid}")
def update_broadcast(bid: int, payload: dict):
    b = bc_svc.update_broadcast(bid, **payload)
    if not b:
        raise HTTPException(status_code=404, detail="not_found")
    return b


@router.delete("/{bid}")
def delete_broadcast(bid: int):
    if not bc_svc.delete_broadcast(bid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@router.post("/{bid}/schedule")
def schedule(bid: int, payload: dict):
    when = payload.get("scheduled_at")
    if not when:
        raise HTTPException(status_code=400, detail="scheduled_at_required")
    return bc_svc.schedule_broadcast(bid, when)


@router.post("/{bid}/cancel")
def cancel(bid: int):
    return bc_svc.cancel_broadcast(bid)


# Stub for Phase 4 — actually sends now (per-link fan-out).
@router.post("/{bid}/send")
def send_now(bid: int):
    return bc_svc.send_broadcast(bid)


@router.get("/{bid}/links")
def list_links(bid: int):
    return bc_svc.list_links(bid)


@router.post("/{bid}/links/{lid}/revoke")
def revoke_link(bid: int, lid: int):
    from broadcaster.services import links as links_svc
    if not links_svc.revoke_link(lid):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


# ── Analytics ────────────────────────────────────────────────

@router.get("/{bid}/analytics")
def analytics(bid: int):
    from broadcaster.services import analytics as analytics_svc
    return analytics_svc.broadcast_analytics(bid)


@router.get("/{bid}/views.csv")
def views_csv(bid: int):
    from broadcaster.services import analytics as analytics_svc
    from fastapi.responses import Response
    return Response(
        content=analytics_svc.raw_views_csv(bid),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="broadcast-{bid}-views.csv"'},
    )


@router.get("/{bid}/comments.csv")
def comments_csv(bid: int):
    """All comments (visible + hidden) for the broadcast — admin-only CSV
    export. Mirrors /{bid}/views.csv in shape and filename convention."""
    from broadcaster.services import comments as comments_svc
    from fastapi.responses import Response
    return Response(
        content=comments_svc.raw_comments_csv(bid),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="broadcast-{bid}-comments.csv"'},
    )
