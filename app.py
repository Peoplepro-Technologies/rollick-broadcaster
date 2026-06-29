"""Rollick Broadcaster — main FastAPI app.

Wires middleware, static files, templates, and routers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

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


templates.env.globals["_status_pill_class"] = _status_pill_class

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
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services.dashboard import dashboard_overview
    overview = dashboard_overview()
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"app_name": get_settings().app_name, "active_nav": "dashboard",
         "admin": {"username": "admin"}, "overview": overview},
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import users as users_svc
    users = users_svc.list_users()
    return templates.TemplateResponse(
        request, "admin/users.html",
        {"app_name": get_settings().app_name, "active_nav": "users",
         "admin": {"username": "admin"}, "users": users},
    )


@app.get("/admin/groups", response_class=HTMLResponse)
def admin_groups_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import groups as groups_svc
    return templates.TemplateResponse(
        request, "admin/groups.html",
        {"app_name": get_settings().app_name, "active_nav": "groups",
         "admin": {"username": "admin"}, "groups": groups_svc.list_groups()},
    )


@app.get("/admin/content", response_class=HTMLResponse)
def admin_content_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import content as content_svc
    items = content_svc.list_content()
    return templates.TemplateResponse(
        request, "admin/content.html",
        {"app_name": get_settings().app_name, "active_nav": "content",
         "admin": {"username": "admin"}, "items": items,
         "texts": [c for c in items if c["content_type"] == "text"],
         "media": [c for c in items if c["content_type"] == "media"]},
    )


@app.get("/admin/broadcasts", response_class=HTMLResponse)
def admin_broadcasts_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import broadcasts as bc_svc
    return templates.TemplateResponse(
        request, "admin/broadcasts_list.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "admin": {"username": "admin"},
         "broadcasts": bc_svc.list_broadcasts()},
    )


@app.get("/admin/broadcasts/new", response_class=HTMLResponse)
def admin_broadcast_new_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import content as content_svc
    from broadcaster.services import groups as groups_svc
    from broadcaster.services import users as users_svc
    return templates.TemplateResponse(
        request, "admin/broadcast_compose.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "admin": {"username": "admin"},
         "content": content_svc.list_content(),
         "groups": groups_svc.list_groups(),
         "users": users_svc.list_users()},
    )


@app.get("/admin/broadcasts/{bid}", response_class=HTMLResponse)
def admin_broadcast_detail_page(request: Request, bid: int):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import broadcasts as bc_svc
    from broadcaster.services import analytics as analytics_svc
    b = bc_svc.get_broadcast(bid)
    if not b:
        return HTMLResponse("Broadcast not found", status_code=404)
    return templates.TemplateResponse(
        request, "admin/broadcast_detail.html",
        {"app_name": get_settings().app_name, "active_nav": "broadcasts",
         "admin": {"username": "admin"},
         "broadcast": b, "links": bc_svc.list_links(bid),
         "analytics": analytics_svc.broadcast_analytics(bid)},
    )


@app.get("/admin/comments", response_class=HTMLResponse)
def admin_comments_page(request: Request, filter: str | None = None):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import comments as comments_svc
    status = "hidden" if filter == "hidden" else "visible"
    return templates.TemplateResponse(
        request, "admin/comments.html",
        {"app_name": get_settings().app_name, "active_nav": "comments",
         "admin": {"username": "admin"},
         "comments": comments_svc.list_all(status=status),
         "filter": filter},
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request):
    if admin_auth.current_admin_id(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    from broadcaster.services import settings as settings_svc
    return templates.TemplateResponse(
        request, "admin/settings.html",
        {"app_name": get_settings().app_name, "active_nav": "settings",
         "admin": {"username": "admin"},
         "settings": settings_svc.all_visible(),
         "base_public_url": get_settings().base_public_url},
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
