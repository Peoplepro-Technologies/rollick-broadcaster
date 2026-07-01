"""Tests for the /admin/admins SSR page (template + nav, no JS)."""
from __future__ import annotations

import json as _json
import re

import pytest


@pytest.fixture
async def authed_super_admin(client):
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )
    return client


async def test_admins_page_renders_200(authed_super_admin):
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    assert r.status_code == 200


async def test_admins_page_lists_existing_admins(authed_super_admin):
    """The HTML must contain every existing admin's username."""
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    body = r.text
    assert "admin" in body  # the bootstrap super_admin


async def test_admins_page_includes_current_admin_meta(authed_super_admin):
    """The `<meta name='current-admin'>` must carry the JSON identity."""
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    body = r.text
    assert 'name="current-admin"' in body
    # Content is wrapped in single quotes; capture ends at the next `'`.
    m = re.search(r"""<meta\s+name=['"]current-admin['"]\s+content='([^']+)'""", body)
    assert m is not None, body
    parsed = _json.loads(m.group(1).replace("&quot;", '"').replace("&#34;", '"'))
    assert parsed["username"] == "admin"
    assert parsed["role"] == "super_admin"
    assert parsed["id"] >= 1


async def test_admins_page_self_account_card(authed_super_admin):
    """The 'Your account' card must show the logged-in user's username."""
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    body = r.text
    assert "Your account" in body
    assert body.count("admin") >= 2


async def test_admins_page_table_has_action_buttons(authed_super_admin):
    """Each admin row has Change role / Change password / Delete buttons."""
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    body = r.text
    assert ">Change role<" in body
    assert ">Change password<" in body
    assert ">Delete<" in body


async def test_admins_page_has_add_admin_button(authed_super_admin):
    r = await authed_super_admin.get("/admin/admins", headers={"Accept": "text/html"})
    assert "+ Add admin" in r.text


# ── API mirror tests for the JS-driven flow ────────────────────────


async def test_create_admin_via_api(authed_super_admin):
    """The JS calls POST /api/admins on form submit; verify the
    endpoint behaves as the JS expects."""
    r = await authed_super_admin.post(
        "/api/admins",
        json={"username": "page_hr", "password": "abcd1234", "role": "hr_admin"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "page_hr"
    assert body["role"] == "hr_admin"


async def test_change_role_via_api(authed_super_admin):
    """JS calls POST /api/admins/{id}/role on the Change role form."""
    r = await authed_super_admin.post(
        "/api/admins",
        json={"username": "role_target", "password": "abcd1234", "role": "hr_admin"},
    )
    aid = r.json()["id"]
    r = await authed_super_admin.post(
        f"/api/admins/{aid}/role", json={"role": "content_admin"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "content_admin"


async def test_change_password_via_api(authed_super_admin):
    r = await authed_super_admin.post(
        "/api/admins",
        json={"username": "pw_target", "password": "first-pass", "role": "hr_admin"},
    )
    aid = r.json()["id"]
    r = await authed_super_admin.post(
        f"/api/admins/{aid}/password", json={"password": "new-pass-1"},
    )
    assert r.status_code == 200


async def test_delete_admin_via_api(authed_super_admin):
    """JS calls DELETE /api/admins/{id} on the Delete form. Create a
    non-super target and delete it (avoids both self-delete and the
    last-super_admin lockout)."""
    r = await authed_super_admin.post(
        "/api/admins",
        json={"username": "del_target", "password": "abcd1234", "role": "hr_admin"},
    )
    target = r.json()["id"]
    r = await authed_super_admin.delete(f"/api/admins/{target}")
    assert r.status_code == 200
