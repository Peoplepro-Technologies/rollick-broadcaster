"""Rollick Broadcaster — main FastAPI app.

Wires middleware, static files, templates, and routers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from broadcaster import __version__
from broadcaster.db import init_db
from broadcaster.settings import get_settings

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "broadcaster" / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Rollick Broadcaster",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"app_name": get_settings().app_name, "active_nav": None},
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
