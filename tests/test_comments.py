"""Phase 5 — anonymous comments with anti-spam."""
from __future__ import annotations

import time

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


async def _make_token(client):
    """Create 1 user + 1 broadcast, return the link token."""
    u = (await client.post("/api/users", json={"name": "X", "phone": "9100000001"})).json()
    b = (await client.post("/api/broadcasts", json={
        "title": "X", "user_ids": [u["id"]],
    })).json()
    links = (await client.get(f"/api/broadcasts/{b['id']}/links")).json()
    return links[0]["token"]


async def _post_comment(client, token, body="Nice!", *, ts_offset=5,
                        website="", with_ts=True):
    """Helper: POST a comment with the standard anti-spam fields."""
    data = {"body": body, "website": website}
    if with_ts:
        ts_ms = int((time.time() - ts_offset) * 1000)
        data["ts_issued"] = str(ts_ms)
    return await client.post(f"/v/{token}/comments", data=data)


# ── Happy path ──────────────────────────────────────────────

async def test_post_comment_succeeds(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token)
    assert r.status_code == 200
    body = r.json()
    assert "id" in body
    assert "created_at" in body


async def test_posted_comment_appears_in_viewer(authed_client, client):
    token = await _make_token(authed_client)
    await _post_comment(client, token, body="Hello world")
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200
    assert "Hello world" in r.text


# ── Body validation ─────────────────────────────────────────

async def test_empty_body_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="")
    assert r.status_code == 422


async def test_too_short_body_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="a")
    assert r.status_code == 422
    assert r.json()["detail"] == "body_too_short"


async def test_too_long_body_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="x" * 501)
    assert r.status_code == 422
    assert r.json()["detail"] == "body_too_long"


async def test_whitespace_only_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="   ")
    assert r.status_code == 422


async def test_too_many_links_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="check https://a.com and https://b.com")
    assert r.status_code == 422
    assert r.json()["detail"] == "too_many_links"


async def test_one_link_allowed(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="see https://example.com")
    assert r.status_code == 200


async def test_profanity_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, body="this is shit")
    assert r.status_code == 422
    assert r.json()["detail"] == "profanity"


# ── Honeypot ─────────────────────────────────────────────────

async def test_honeypot_filled_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, website="http://spam.example.com")
    assert r.status_code == 400
    assert r.json()["detail"] == "bot_detected"


# ── Time-to-fill ────────────────────────────────────────────

async def test_submitted_too_fast_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, ts_offset=0)  # same second as page load
    assert r.status_code == 400
    assert r.json()["detail"] == "submitted_too_fast"


async def test_stale_form_rejected(authed_client, client):
    token = await _make_token(authed_client)
    r = await _post_comment(client, token, ts_offset=100000)  # > 2h
    assert r.status_code == 400
    assert r.json()["detail"] == "stale_form"


# ── Rate limits ──────────────────────────────────────────────

async def test_per_token_cap(authed_client, client):
    token = await _make_token(authed_client)
    # Post up to the cap (default 3)
    for i in range(3):
        r = await _post_comment(client, token, body=f"Comment {i}")
        assert r.status_code == 200, f"failed at {i}: {r.text}"
    # 4th should fail
    r4 = await _post_comment(client, token, body="4th")
    assert r4.status_code == 429
    assert r4.json()["detail"] == "per_token_cap_exceeded"


async def test_per_session_cooldown(authed_client, client, monkeypatch):
    """Cooldown is timing-based; conftest sets it to 0 for other tests,
    so we explicitly set it back to 30 and verify a back-to-back post is blocked."""
    monkeypatch.setenv("COMMENT_COOLDOWN_SECONDS", "30")
    from broadcaster.settings import bust_settings_cache
    bust_settings_cache()
    token = await _make_token(authed_client)
    r1 = await _post_comment(client, token, body="First")
    assert r1.status_code == 200
    r2 = await _post_comment(client, token, body="Second")
    assert r2.status_code == 429
    assert r2.json()["detail"] == "cooldown"


# ── Link state ──────────────────────────────────────────────

async def test_expired_link_rejects_comment(authed_client, client):
    token = await _make_token(authed_client)
    # Get the link id
    with __import__("broadcaster").db.get_db() as conn:
        # Easier: just expire the link directly
        from broadcaster.db import get_db
        with get_db() as conn:
            conn.execute(
                "UPDATE broadcast_links SET expires_at = '2020-01-01T00:00:00+00:00' "
                "WHERE token = ?", (token,),
            )
    r = await _post_comment(client, token)
    assert r.status_code == 410


# ── GET /comments polling ──────────────────────────────────

async def test_get_comments_returns_visible_only(authed_client, client):
    token = await _make_token(authed_client)
    await _post_comment(client, token, body="Visible comment")
    r = await client.get(f"/v/{token}/comments")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["body"] == "Visible comment"


# ── No auth required ───────────────────────────────────────

async def test_comment_post_no_auth(authed_client, client):
    """Subscribers post without login."""
    token = await _make_token(authed_client)  # uses authed to create
    # Post as unauthed client
    r = await _post_comment(client, token, body="Anonymous!")
    assert r.status_code == 200
