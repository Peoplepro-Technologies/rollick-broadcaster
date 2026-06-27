"""Phase 1a — admin auth.

Covers bootstrap, login (form + JSON), logout, /me, and require_admin.
"""
from __future__ import annotations

import pytest


async def test_bootstrap_creates_default_admin(app):
    """Lifespan should have created one admin from env credentials."""
    from broadcaster.db import get_db
    with get_db() as conn:
        rows = conn.execute("SELECT username FROM admins").fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] == "admin"


async def test_login_with_valid_credentials_sets_session(client):
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["redirect"] == "/admin/"


async def test_login_with_wrong_password_rejects(client):
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "wrong"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


async def test_login_with_unknown_user_rejects(client):
    r = await client.post(
        "/api/auth/login",
        data={"username": "nobody", "password": "whatever"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 401


async def test_login_form_redirects_to_dashboard(client):
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/"


async def test_login_form_redirects_to_login_on_bad_creds(client):
    r = await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "wrong"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?error=1"


async def test_me_requires_session(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


async def test_me_returns_current_admin_after_login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert "id" in body


async def test_logout_clears_session(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    r = await client.post("/api/auth/logout", headers={"Accept": "application/json"})
    assert r.status_code == 200

    r2 = await client.get("/api/auth/me")
    assert r2.status_code == 401


async def test_admin_dashboard_requires_session(client):
    r = await client.get("/admin/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


async def test_admin_dashboard_reachable_after_login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    r = await client.get("/admin/", follow_redirects=False)
    assert r.status_code == 200
    assert b"dashboard" in r.content.lower()
