"""Phase 8 — settings + security hardening."""
from __future__ import annotations

import pytest


async def _login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


# ── Settings K/V ─────────────────────────────────────────────

async def test_get_settings_empty(authed_client):
    r = await authed_client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {}


async def test_set_and_get_settings(authed_client):
    await authed_client.post("/api/settings", json={
        "app_brand_name": "Acme", "link_token_ttl_days": "7",
    })
    r = await authed_client.get("/api/settings")
    body = r.json()
    assert body["app_brand_name"] == "Acme"
    assert body["link_token_ttl_days"] == "7"


async def test_settings_persists_across_calls(authed_client):
    await authed_client.post("/api/settings", json={"app_brand_name": "X"})
    await authed_client.post("/api/settings", json={"app_brand_name": "Y"})
    r = await authed_client.get("/api/settings")
    assert r.json()["app_brand_name"] == "Y"


async def test_settings_rejects_server_secrets(authed_client):
    """Server-internal secrets (session_secret, ip_hash_pepper,
    media_sign_secret) must NEVER be settable from the UI. User-supplied
    credentials (smtp_pass, whatsapp_access_token, whatsapp_app_secret)
    ARE settable — they're stored in the DB."""
    r = await authed_client.post("/api/settings", json={
        "session_secret": "leaked", "ip_hash_pepper": "leaked",
        "media_sign_secret": "leaked", "app_brand_name": "OK",
    })
    body = r.json()
    assert body["rejected"] == ["session_secret", "ip_hash_pepper",
                                 "media_sign_secret"]
    assert body["saved"] == 1
    # Confirm none of the rejected secrets made it to the DB
    r2 = await authed_client.get("/api/settings")
    keys = r2.json().keys()
    assert "session_secret" not in keys
    assert "ip_hash_pepper" not in keys
    assert "media_sign_secret" not in keys


# ── SMTP/WhatsApp test buttons ─────────────────────────────

async def test_test_smtp_rejects_when_not_configured(authed_client):
    """No SMTP_HOST in test env."""
    r = await authed_client.post("/api/settings/test-smtp")
    assert r.status_code == 400
    assert r.json()["detail"] == "smtp_not_configured"


async def test_test_whatsapp_rejects_when_not_configured(authed_client):
    r = await authed_client.post("/api/settings/test-whatsapp")
    assert r.status_code == 400
    assert r.json()["detail"] == "whatsapp_not_configured"


# ── Security headers (CSP) ─────────────────────────────────

async def test_csp_header_present(client):
    r = await client.get("/api/health")
    csp = r.headers.get("content-security-policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "https://cdn.jsdelivr.net" in csp


async def test_x_content_type_options_header(client):
    r = await client.get("/api/health")
    assert r.headers.get("x-content-type-options") == "nosniff"


async def test_referrer_policy_header(client):
    r = await client.get("/api/health")
    assert r.headers.get("referrer-policy") == "no-referrer"


async def test_csp_applies_to_viewer(client):
    """Public viewer also gets CSP — third-party scripts can't inject."""
    # First need a valid token; create one
    from broadcaster.services import users as users_svc
    from broadcaster.services import broadcasts as bc_svc
    u = users_svc.create_user(name="A", phone="7100000001")
    b = bc_svc.create_broadcast(title="X", user_ids=[u["id"]])
    from broadcaster.db import get_db
    with get_db() as conn:
        link = conn.execute("SELECT token FROM broadcast_links WHERE broadcast_id = ?", (b["id"],)).fetchone()
    token = link["token"]
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


# ── Auth ─────────────────────────────────────────────────────

async def test_settings_require_auth(client):
    r = await client.get("/api/settings")
    assert r.status_code == 401
    r = await client.post("/api/settings", json={"x": "y"})
    assert r.status_code == 401


# ── runtime_overrides + admin settings page ────────────────

async def test_settings_page_renders(authed_client):
    """The /admin/settings page must not 500 because runtime context is
    missing — the template references {{ runtime.smtp_host }} et al."""
    r = await authed_client.get("/admin/settings")
    assert r.status_code == 200
    html = r.text
    assert 'name="smtp_host"' in html
    assert 'name="smtp_pass"' in html
    assert 'name="whatsapp_phone_id"' in html
    assert 'name="whatsapp_access_token"' in html


async def test_runtime_endpoint_returns_expected_keys(authed_client):
    r = await authed_client.get("/api/settings/runtime")
    assert r.status_code == 200
    body = r.json()
    for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                "smtp_pass", "whatsapp_phone_id", "whatsapp_api_version",
                "whatsapp_country_code", "whatsapp_access_token",
                "whatsapp_app_secret"):
        assert key in body, f"missing key: {key}"


def test_runtime_overrides_helper_shape():
    from broadcaster.services import settings as settings_svc
    body = settings_svc.runtime_overrides()
    assert isinstance(body, dict)
    for key in ("smtp_host", "smtp_pass", "whatsapp_phone_id",
                "whatsapp_access_token", "whatsapp_app_secret"):
        assert key in body, f"helper missing key: {key}"


# ── Cache behaviour ──────────────────────────────────────────────
# get_settings() is lru_cache()'d on the merged Settings. Admin writes
# must invalidate so live updates take effect without a server restart.

async def test_settings_cache_picks_up_writes_without_restart(authed_client):
    """After POST /api/settings writes a value, the next get_settings()
    call MUST return the merged result without any cache poisoning.

    Uses link_token_ttl_days — a real Settings field — since some DB
    keys (like app_brand_name) are stored but never applied to the
    Settings model."""
    from broadcaster.settings import bust_settings_cache, get_settings

    bust_settings_cache()
    before = get_settings().link_token_ttl_days

    r = await authed_client.post("/api/settings", json={"link_token_ttl_days": "42"})
    assert r.status_code == 200

    # The route's bust_settings_cache() must clear the cache so this
    # returns the freshly-written value, not the old cached one.
    after = get_settings().link_token_ttl_days
    assert after == 42
    assert after != before or before == 42  # cover both branches


def test_bust_settings_cache_clears_both_layers():
    """bust_settings_cache() must clear the env cache AND the merged
    runtime cache — otherwise DB overrides written before cache would
    never surface through get_settings()."""
    from broadcaster.settings import _env_settings, bust_settings_cache, get_settings
    get_settings()  # warm
    assert _env_settings.cache_info().currsize >= 1
    assert get_settings.cache_info().currsize >= 1
    bust_settings_cache()
    assert _env_settings.cache_info().currsize == 0
    assert get_settings.cache_info().currsize == 0
