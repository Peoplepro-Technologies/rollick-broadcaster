"""Rollick Broadcaster — main FastAPI app.

Wires middleware, static files, templates, and routers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from broadcaster import __version__
from broadcaster.db import init_db
from broadcaster.routes import admin_auth, admin_users, admin_groups, admin_content, admin_broadcasts, admin_comments, admin_settings, viewer
from broadcaster.services import admin as admin_svc
from broadcaster.services import scheduler as sched_svc
from broadcaster.settings import get_settings

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "broadcaster" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    admin_svc.bootstrap_admin()
    sched_svc.start()
    try:
        yield
    finally:
        sched_svc.shutdown()


app = FastAPI(
    title="Rollick Broadcaster",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# Session cookies signed with SESSION_SECRET. SameSite=lax is the
# default; admin SPA needs cross-route GETs to carry the cookie.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret,
    session_cookie="broadcaster_session",
    same_site="lax",
    https_only=False,  # flip to True in production behind HTTPS
)


# ── Phase 8: security headers ────────────────────────────────

@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    # CSP — same-origin default, allow Google Fonts (admin/login use Inter),
    # and our own media routes. Public viewer media src is from our own host.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # HSTS only makes sense behind HTTPS — leave commented for the
    # developer to enable in their reverse proxy.
    # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _validate_filters(query_params) -> tuple[dict, Optional[str]]:
    """Read category/channel/date_range filter query params and return
    (cleaned_filters, flash_message_or_None).

    Rules (see docs/superpowers/specs/2026-06-30-broadcast-analytics-
    filtering-design.md for the authoritative definitions):

      - Empty / whitespace values are dropped.
      - Unknown category values are dropped (no error).
      - Unknown channel values are dropped (no error).
      - date_from > date_to  → both dropped, flash.
      - Only one date bound  → both dropped, flash.
      - Unparseable dates    → treated as absent (no flash).
    """
    category = (query_params.get("category") or "").strip()
    channel = (query_params.get("channel") or "").strip()
    date_from = (query_params.get("date_from") or "").strip()
    date_to = (query_params.get("date_to") or "").strip()
    q = (query_params.get("q") or "").strip()

    flash: Optional[str] = None
    cleaned = {
        "category": category,
        "channel": channel,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
    }

    if (date_from and not date_to) or (date_to and not date_from):
        cleaned["date_from"] = ""
        cleaned["date_to"] = ""
        flash = "Pick both dates or leave both empty."
    elif date_from and date_to:
        try:
            from datetime import date as _date
            d_from = _date.fromisoformat(date_from)
            d_to = _date.fromisoformat(date_to)
        except (TypeError, ValueError):
            cleaned["date_from"] = ""
            cleaned["date_to"] = ""
            flash = "Date inputs must be valid dates."
        else:
            if d_from > d_to:
                cleaned["date_from"] = ""
                cleaned["date_to"] = ""
                flash = f"date_from ({date_from}) cannot be after date_to ({date_to})."

    # Drop unknown category silently (no flash — could be a hand-edited URL).
    if cleaned["category"]:
        from broadcaster.services import broadcasts as _bc
        valid_categories = set(_bc.distinct_categories())
        if cleaned["category"] not in valid_categories:
            cleaned["category"] = ""

    # Drop unknown channel silently (whitelist).
    if cleaned["channel"] and cleaned["channel"] not in ("whatsapp", "email", "both"):
        cleaned["channel"] = ""

    return cleaned, flash


def _status_pill_class(status: str) -> str:
    """Map a broadcast status string to an admin.css .pill modifier class."""
    return {
        "sent": "success",
        "queued": "info",
        "scheduled": "info",
        "draft": "muted",
        "sending": "warning",
        "failed": "danger",
        "cancelled": "muted",
    }.get(status, "muted")


def _to_ist(s: str | None) -> str:
    """Convert a UTC ISO datetime string to IST (UTC+5:30) as 'YYYY-MM-DD HH:MM'.

    Pass-through on None / parse error so callers can use `value | to_ist` safely.
    """
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s[:19])
        return (dt + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return s


templates.env.globals["_status_pill_class"] = _status_pill_class
templates.env.filters["to_ist"] = _to_ist

app.include_router(admin_auth.router)
app.include_router(admin_users.router)
app.include_router(admin_groups.router)
app.include_router(admin_content.router)
app.include_router(admin_broadcasts.router)
app.include_router(admin_comments.router)
app.include_router(admin_settings.router)
viewer.set_templates(templates)
app.include_router(viewer.router)


# ── Public routes ───────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    """Liveness probe + version. No auth."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
    }


# ── Admin + viewer pages (Jinja-rendered) ───────────────────────
# Routers are added per-phase. For Phase 0 we ship a minimal login
# placeholder so the app renders something end-to-end.

def _page_admin(request: Request, *allowed: str):
    """Gate for Jinja-rendered admin pages.

    Returns ("ok", AdminUser) when the session is valid AND the role
    is in `allowed`. Returns ("redirect", Response) when there's no
    valid session; the Response is a 303 to /admin/login. Returns
    ("forbidden", AdminUser) when the session is valid but the role
    doesn't match — the caller must render admin/403.html with the
    `AdminUser` (so the user knows who they are).
    """
    from broadcaster.rbac import AdminUser
    admin_id = admin_auth.current_admin_id(request)
    if admin_id is None:
        return ("redirect", RedirectResponse("/admin/login", status_code=303))
    row = admin_svc.find_by_id(admin_id)
    if row is None:
        return ("redirect", RedirectResponse("/admin/login", status_code=303))
    user = AdminUser(id=row["id"], username=row["username"], role=row["role"])
    if user.role not in allowed:
        return ("forbidden", user)
    return ("ok", user)


def _render_403(request: Request, admin, active_nav: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/403.html",
        {
            "app_name": get_settings().app_name,
            "active_nav": active_nav,
            "current_admin": admin,
        },
        status_code=403,
    )


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request) -> HTMLResponse:
    error = request.query_params.get("error") == "1"
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"app_name": get_settings().app_name, "active_nav": None, "error": error},
    )


@app.get("/admin/", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    state, value = _page_admin(request, "super_admin", "hr_admin", "content_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "dashboard")
    admin = value
    from broadcaster.services.dashboard import dashboard_overview
    overview = dashboard_overview()
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"app_name": get_settings().app_name, "active_nav": "dashboard",
         "current_admin": admin, "overview": overview},
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    state, value = _page_admin(request, "super_admin", "hr_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "users")
    admin = value
    from broadcaster.services import users as users_svc
    users = users_svc.list_users()
    return templates.TemplateResponse(
        request, "admin/users.html",
        {"app_name": get_settings().app_name, "active_nav": "users",
         "current_admin": admin, "users": users},
    )


@app.get("/admin/groups", response_class=HTMLResponse)
def admin_groups_page(request: Request):
    state, value = _page_admin(request, "super_admin", "hr_admin")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "groups")
    admin = value
    from broadcaster.services import groups as groups_svc
    return templates.TemplateResponse(
        request, "admin/groups.html",
        {"app_name": get_settings().app_name, "active_nav": "groups",
         "current_admin": admin, "groups": groups_svc.list_groups()},
    )


@app.get("/admin/content", response_class=HTMLResponse)
def admin_content_page(request: Request):
    state, value = _page_admin(request, "super_admin", "content_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "content")
    admin = value
    from broadcaster.services import content as content_svc
    items = content_svc.list_content()
    return templates.TemplateResponse(
        request, "admin/content.html",
        {"app_name": get_settings().app_name, "active_nav": "content",
         "current_admin": admin, "items": items,
         "texts": [c for c in items if c["content_type"] == "text"],
         "media": [c for c in items if c["content_type"] == "media"]},
    )


@app.get("/admin/broadcasts", response_class=HTMLResponse)
def admin_broadcasts_page(request: Request):
    state, value = _page_admin(request, "super_admin", "hr_admin", "content_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "broadcasts")
    admin = value
    from broadcaster.services import broadcasts as bc_svc

    filters, filter_flash = _validate_filters(request.query_params)

    # If the user was bounced here from a deleted/missing broadcast,
    # show a one-shot message so they know what happened.
    flash = filter_flash
    if not flash:
        missing_id = request.query_params.get("missing")
        if missing_id:
            flash = f"Broadcast #{missing_id} no longer exists (it may have been deleted)."

    broadcasts = bc_svc.list_broadcasts(
        category=filters["category"] or None,
        channel=filters["channel"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
        q=filters["q"] or None,
    )
    counts = bc_svc.count_broadcasts_by_category_channel(
        category=filters["category"] or None,
        channel=filters["channel"] or None,
        date_from=filters["date_from"] or None,
        date_to=filters["date_to"] or None,
        q=filters["q"] or None,
    )
    category_options = bc_svc.distinct_categories()
    applied = {
        "category": filters["category"],
        "channel": filters["channel"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "q": filters["q"],
    }
    return templates.TemplateResponse(
        request, "admin/broadcasts_list.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "current_admin": admin,
         "broadcasts": broadcasts, "counts": counts,
         "applied": applied,
         "category_options": category_options,
         "channel_options": ["whatsapp", "email", "both"],
         "flash": flash},
    )


@app.get("/admin/broadcasts/new", response_class=HTMLResponse)
def admin_broadcast_new_page(request: Request):
    state, value = _page_admin(request, "super_admin", "content_admin")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "broadcasts")
    admin = value
    from broadcaster.services import content as content_svc
    from broadcaster.services import groups as groups_svc
    from broadcaster.services import users as users_svc
    return templates.TemplateResponse(
        request, "admin/broadcast_compose.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "current_admin": admin,
         "content": content_svc.list_content(),
         "groups": groups_svc.list_groups(),
         "users": users_svc.list_users()},
    )


@app.get("/admin/broadcasts/{bid}", response_class=HTMLResponse)
def admin_broadcast_detail_page(request: Request, bid: int):
    state, value = _page_admin(request, "super_admin", "hr_admin", "content_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "broadcasts")
    admin = value
    from broadcaster.services import broadcasts as bc_svc
    from broadcaster.services import analytics as analytics_svc
    from broadcaster.services import comments as comments_svc
    b = bc_svc.get_broadcast(bid)
    if not b:
        # Broadcast was deleted (or never existed / link went stale).
        # Don't dump the user on a raw 404 page — bounce them to the
        # list with a one-shot flash that explains what happened.
        resp = RedirectResponse("/admin/broadcasts?missing=" + str(bid), status_code=303)
        return resp
    return templates.TemplateResponse(
        request, "admin/broadcast_detail.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "current_admin": admin,
         "broadcast": b, "links": bc_svc.list_links(bid),
         "comments": comments_svc.list_for_broadcast(bid, status=None, q=None),
         "analytics": analytics_svc.broadcast_analytics(bid)},
    )


@app.get("/admin/comments", response_class=HTMLResponse)
def admin_comments_page(request: Request, filter: str | None = None):
    state, value = _page_admin(request, "super_admin", "content_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "comments")
    admin = value
    from broadcaster.services import comments as comments_svc
    status = "hidden" if filter == "hidden" else "visible"
    return templates.TemplateResponse(
        request, "admin/comments.html",
        {"app_name": get_settings().app_name, "active_nav": "comments",
         "current_admin": admin,
         "comments": comments_svc.list_all(status=status),
         "filter": filter},
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request):
    state, value = _page_admin(request, "super_admin", "management")
    if state == "redirect":
        return value
    if state == "forbidden":
        return _render_403(request, value, "settings")
    admin = value
    from broadcaster.services import settings as settings_svc
    return templates.TemplateResponse(
        request, "admin/settings.html",
        {"app_name": get_settings().app_name, "active_nav": "settings",
         "current_admin": admin,
         "settings": settings_svc.all_visible(),
         "runtime": settings_svc.runtime_overrides(),
         "base_public_url": get_settings().base_public_url,
         "is_secret_keys": settings_svc.secret_keys()},
    )


@app.get("/")
def root() -> dict:
    return {
        "app": get_settings().app_name,
        "version": __version__,
        "admin": "/admin/login",
        "viewer": "/v/{token}",
        "health": "/api/health",
        "docs": "/api/docs",
    }
