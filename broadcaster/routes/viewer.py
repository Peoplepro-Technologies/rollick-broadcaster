"""Public viewer router — /v/{token} (no auth).

This is the link the subscriber clicks in their WhatsApp/email. The
viewer is fully public: no login, no admin token. The URL token IS
the credential. Tokens are 192-bit, opaque, scoped to (broadcast, user).

Endpoints (Phase 3):
  GET  /v/{token}           — SSR viewer page
  POST /v/{token}/view      — idempotent first-view marker
  GET  /v/{token}/media     — serve the broadcast's media (or 404 if none)

Phase 5 adds POST /v/{token}/comments.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from broadcaster.db import get_db
from broadcaster.services import links as links_svc
from broadcaster.services import views as views_svc
from broadcaster.settings import get_settings
from pathlib import Path

router = APIRouter(prefix="/v", tags=["viewer"])

# Templates dir for viewer — uses the same Jinja env as the admin app.
# We import the templates instance from app.py at module load via a
# small helper to avoid circular imports.
_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Trust X-Forwarded-For only behind a known proxy."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


@router.get("/{token}", response_class=HTMLResponse)
def viewer_page(request: Request, token: str):
    link = links_svc.resolve_token(token)
    if not link:
        return _templates.TemplateResponse(  # type: ignore[union-attr]
            request, "viewer/expired.html",
            {"reason": "expired_or_revoked"}, status_code=410,
        )

    # Record the view (idempotent on first_viewed_at; always appends a row)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    referrer = request.headers.get("referer")
    views_svc.record_view(link["id"], ip=ip, ua=ua, referrer=referrer)

    # Fetch media metadata if a content_id is attached
    media = None
    if link.get("content_id"):
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, file_name, mime_type, content_data FROM content WHERE id = ?",
                (link["content_id"],),
            ).fetchone()
        if row:
            media = dict(row)

    # Comments list (read-only for v1; Phase 5 wires the form)
    with get_db() as conn:
        comments = conn.execute(
            "SELECT id, body, created_at FROM comments "
            "WHERE link_id = ? AND status = 'visible' ORDER BY created_at DESC LIMIT 20",
            (link["id"],),
        ).fetchall()
    comments = [dict(c) for c in comments]

    return _templates.TemplateResponse(  # type: ignore[union-attr]
        request, "viewer/page.html",
        {
            "link": link,
            "media": media,
            "comments": comments,
            "comment_count": len(comments),
            "base_public_url": get_settings().base_public_url,
        },
    )


@router.post("/{token}/view")
def mark_viewed(request: Request, token: str):
    link = links_svc.resolve_token(token)
    if not link:
        return JSONResponse({"error": "link_expired"}, status_code=410)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    referrer = request.headers.get("referer")
    info = views_svc.record_view(link["id"], ip=ip, ua=ua, referrer=referrer)
    return {"ok": True, **info}


@router.get("/{token}/media")
def viewer_media(request: Request, token: str):
    link = links_svc.resolve_token(token)
    if not link:
        return JSONResponse({"error": "link_expired"}, status_code=410)
    if not link.get("content_id"):
        return JSONResponse({"error": "no_media"}, status_code=404)
    with get_db() as conn:
        row = conn.execute(
            "SELECT file_name, mime_type, content_data FROM content WHERE id = ?",
            (link["content_id"],),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "media_missing"}, status_code=404)
    path = Path(row["content_data"])
    if not path.exists():
        return JSONResponse({"error": "file_missing"}, status_code=404)
    # Support HTTP Range for video scrubbing
    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=row["file_name"] or path.name,
    )
