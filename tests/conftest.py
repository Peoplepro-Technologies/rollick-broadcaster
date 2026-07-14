"""Test fixtures.

Each test runs against a fresh in-memory or tmp-path SQLite DB to keep
tests isolated. The FastAPI app is exercised via httpx.AsyncClient with
the ASGI transport — no live server.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


# Point settings at a fresh temp DB before app modules are imported.
# Using a per-session file rather than :memory: so PRAGMA + FK work
# the same as production.
@pytest.fixture
def test_db_path(tmp_path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture(autouse=True)
def _isolate_db(test_db_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", str(test_db_path))
    monkeypatch.setenv("SESSION_SECRET", "test-secret-32-chars-or-more-please")
    monkeypatch.setenv("IP_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pass")
    # Tests don't need cooldown timing — set to 0 so per-token-cap
    # and other layered checks can be exercised in tight loops.
    monkeypatch.setenv("COMMENT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("COMMENT_MAX_PER_LINK_LIFETIME", "3")
    # Force MockSender for tests — real SMTP/WA creds in .env must NOT
    # leak through or tests would try to call smtp.office365.com.
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASS", "")
    monkeypatch.setenv("SMTP_FROM", "")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    monkeypatch.setenv("AISENSY_API_KEY", "")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "")
    # Force re-init since settings are cached.
    from broadcaster.settings import bust_settings_cache
    from broadcaster.db import init_db
    from broadcaster.services.admin import bootstrap_admin
    from broadcaster.services.scheduler import shutdown
    bust_settings_cache()
    init_db()
    bootstrap_admin()
    # Reset the scheduler singleton between tests so job state from a
    # previous test doesn't leak.
    try:
        shutdown()
    except Exception:
        pass
    import broadcaster.services.scheduler as sched_mod
    sched_mod._scheduler = None
    sched_mod._started = False
    yield


@pytest.fixture
def app():
    # Imported lazily so the env-var swap above takes effect first.
    from app import app as fastapi_app
    return fastapi_app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Shared fixtures for the password-recovery flow ─────────────────
# The autouse _isolate_db fixture above zeros SMTP_HOST and SMTP_FROM
# so the no-config branches of the forgot-password service are the
# default. Tests that want the happy path enable this fixture; it sets
# the global recovery mailbox AND the SMTP env vars.
@pytest.fixture
def recovery_settings(monkeypatch):
    """Configure the global recovery mailbox + SMTP env vars.

    Returns the `broadcaster.services.settings` module so tests can call
    `settings_svc.set_("password_recovery_email", "...")` against it.
    """
    from broadcaster.services import settings as settings_svc
    from broadcaster.settings import bust_settings_cache
    settings_svc.set_("password_recovery_email", "it-test@rollick.co.in")
    monkeypatch.setenv("SMTP_HOST", "smtp.test.example")
    monkeypatch.setenv("SMTP_FROM", "noreply@rollick.co.in")
    bust_settings_cache()
    return settings_svc
