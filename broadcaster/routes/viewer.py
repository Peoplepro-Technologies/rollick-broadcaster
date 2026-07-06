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

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from broadcaster.db import get_db
from broadcaster.services import antispam
from broadcaster.services import comments as comments_svc
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


def _render_viewer_message(link: dict, token: str) -> str:
    """Substitute {{viewer_link}} / {{link}} in the broadcast's message_text
    so the SSR viewer shows the same link the email/WhatsApp body got.
    Mirrors `broadcasts._render_message` but without auto-appending the
    link when missing (the viewer is the link itself; no point appending).
    """
    base = get_settings().base_public_url.rstrip("/")
    viewer_link = f"{base}/v/{token}"
    body = (link.get("message_text") or "").strip()
    body = body.replace("{{viewer_link}}", viewer_link).replace("{{link}}", viewer_link)
    return body


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

    # Fetch content metadata if a content_id is attached. Two distinct
    # shapes live in `content`:
    #   - content_type='media' → content_data is an absolute filesystem path
    #     to the uploaded file. Even if the row exists, the file may be
    #     gone (volume reset, manual cleanup) — detect that here so the
    #     template doesn't render a broken <video> and the user gets the
    #     "Media unavailable" notice instead of the browser's cryptic
    #     "No video with supported format and MIME type found".
    #   - content_type='text'  → content_data IS the message body (no
    #     file on disk). The file-existence check must NOT run for
    #     this branch, otherwise every text snippet is misreported as
    #     missing media.
    media = None
    media_unavailable = False
    text_body: str | None = None
    text_caption: str | None = None
    if link.get("content_id"):
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, content_type, caption, file_name, mime_type, content_data "
                "FROM content WHERE id = ?",
                (link["content_id"],),
            ).fetchone()
        if row:
            from broadcaster.services.content import _resolve_content_path
            ctype = row["content_type"]
            if ctype == "media":
                file_path = _resolve_content_path(row["content_data"])
                if file_path.exists():
                    media = {
                        "id": row["id"],
                        "file_name": row["file_name"],
                        "mime_type": row["mime_type"],
                    }
                else:
                    media_unavailable = True
            elif ctype == "text":
                text_body = row["content_data"]
                text_caption = row["caption"]
            # Unknown / future content types: render nothing (silently).

    # Comments list (read-only for v1; Phase 5 wires the form)
    with get_db() as conn:
        comments = conn.execute(
            "SELECT id, body, author_hint, created_at FROM comments "
            "WHERE link_id = ? AND status = 'visible' ORDER BY created_at DESC LIMIT 20",
            (link["id"],),
        ).fetchall()
    comments = [dict(c) for c in comments]

    return _templates.TemplateResponse(  # type: ignore[union-attr]
        request, "viewer/page.html",
        {
            "link": link,
            "media": media,
            "media_unavailable": media_unavailable,
            "text_body": text_body,
            "text_caption": text_caption,
            "comments": comments,
            "comment_count": len(comments),
            "base_public_url": get_settings().base_public_url,
            "rendered_message": _render_viewer_message(link, token),
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
    from broadcaster.services.content import _resolve_content_path
    path = _resolve_content_path(row["content_data"])
    if not path.exists():
        return JSONResponse({"error": "file_missing"}, status_code=404)
    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=row["file_name"] or path.name,
    )


# ── Phase 5: anonymous comments ──────────────────────────────

@router.post("/{token}/comments")
def post_comment(
    request: Request,
    token: str,
    body: str = Form(...),
    website: str = Form(default=""),       # honeypot
    ts_issued: str = Form(default=""),     # millis when page rendered
):
    link = links_svc.resolve_token(token)
    if not link:
        raise HTTPException(status_code=410, detail="link_expired")

    # Honeypot
    if not antispam.check_honeypot(website):
        raise HTTPException(status_code=400, detail="bot_detected")

    # Time-to-fill
    try:
        ts_int = int(ts_issued) if ts_issued else None
    except ValueError:
        ts_int = None
    ok, reason = antispam.check_time_to_fill(ts_int)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    # Body validation
    ok, body_or_reason = antispam.validate_body(body)
    if not ok:
        raise HTTPException(status_code=422, detail=body_or_reason)
    body = body_or_reason

    # Rate limits
    ip = _client_ip(request)
    ip_hash = antispam.hash_for_ip(ip)

    ok, reason = antispam.check_per_token_cap(link["id"])
    if not ok:
        raise HTTPException(status_code=429, detail=reason)

    ok, reason = antispam.check_cooldown(link["id"])
    if not ok:
        raise HTTPException(status_code=429, detail=reason)

    ok, reason = antispam.check_per_ip_rate(ip_hash, link["broadcast_id"])
    if not ok:
        raise HTTPException(status_code=429, detail=reason)

    comment = comments_svc.create_comment(
        link_id=link["id"],
        broadcast_id=link["broadcast_id"],
        body=body,
        ip_hash=ip_hash,
    )
    return {
        "id": comment["id"],
        "created_at": comment["created_at"],
        "cooldown_remaining_s": get_settings().comment_cooldown_seconds,
    }


@router.get("/{token}/comments")
def list_comments(request: Request, token: str):
    """Optional polling endpoint — server-side rendered HTML already
    includes the initial list."""
    link = links_svc.resolve_token(token)
    if not link:
        raise HTTPException(status_code=410, detail="link_expired")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, body, author_hint, created_at FROM comments "
            "WHERE link_id = ? AND status = 'visible' ORDER BY created_at DESC LIMIT 20",
            (link["id"],),
        ).fetchall()
    return [dict(r) for r in rows]
