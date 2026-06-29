"""Phase 3 — Public viewer resolve."""
from __future__ import annotations

import io

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


async def _make_user_and_broadcast(client, *, content_id: int | None = None):
    """Helper: 1 active user, 1 broadcast with link, return (token, link_id, user)."""
    u = (await client.post("/api/users", json={"name": "X", "phone": "9100000001"})).json()
    b = (await client.post("/api/broadcasts", json={
        "title": "Hello viewer", "message_text": "Body here",
        "user_ids": [u["id"]],
        **({"content_id": content_id} if content_id else {}),
    })).json()
    bid = b["id"]
    links = (await client.get(f"/api/broadcasts/{bid}/links")).json()
    return links[0]["token"], links[0]["id"], u


# ── Page render ──────────────────────────────────────────────

async def test_viewer_page_returns_html(authed_client, client):
    token, _, _ = await _make_user_and_broadcast(authed_client)
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200
    assert "Hello viewer" in r.text
    assert "Body here" in r.text


async def test_viewer_page_renders_video_when_media(authed_client, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("clip.mp4", io.BytesIO(b"fake-video-bytes"), "video/mp4")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]
    token, _, _ = await _make_user_and_broadcast(authed_client, content_id=cid)
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200
    assert "<video" in r.text
    assert "clip.mp4" in r.text or "video/mp4" in r.text


async def test_viewer_page_renders_image_when_media(authed_client, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("pic.png", io.BytesIO(b"\x89PNG_FAKE"), "image/png")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]
    token, _, _ = await _make_user_and_broadcast(authed_client, content_id=cid)
    r = await client.get(f"/v/{token}")
    assert "<img" in r.text


async def test_viewer_page_includes_comments_block(authed_client, client):
    token, _, _ = await _make_user_and_broadcast(authed_client)
    r = await client.get(f"/v/{token}")
    assert "Comments" in r.text
    assert "comment" in r.text.lower()


# ── Token resolution ────────────────────────────────────────

async def test_unknown_token_returns_410(client):
    r = await client.get("/v/totally-not-a-real-token-1234")
    assert r.status_code == 410


async def test_revoked_token_returns_410(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    r = await authed_client.get("/api/broadcasts")
    bid = r.json()[0]["id"]
    await authed_client.post(f"/api/broadcasts/{bid}/links/{link_id}/revoke")
    r2 = await client.get(f"/v/{token}")
    assert r2.status_code == 410


async def test_expired_token_returns_410(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE broadcast_links SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (link_id,),
        )
    r = await client.get(f"/v/{token}")
    assert r.status_code == 410


# ── View tracking ────────────────────────────────────────────

async def test_view_records_first_viewed_at(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    r1 = await client.get(f"/v/{token}")
    assert r1.status_code == 200
    from broadcaster.db import get_db
    with get_db() as conn:
        row = conn.execute("SELECT first_viewed_at FROM broadcast_links WHERE id = ?", (link_id,)).fetchone()
    assert row["first_viewed_at"] is not None


async def test_view_inserts_link_views_row(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    await client.get(f"/v/{token}")
    from broadcaster.db import get_db
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM link_views WHERE link_id = ?", (link_id,)).fetchone()["n"]
    assert n >= 1


async def test_view_hashes_ip_and_ua(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    await client.get(f"/v/{token}", headers={"User-Agent": "TestBrowser/1.0"})
    from broadcaster.db import get_db
    with get_db() as conn:
        row = conn.execute("SELECT ip_hash, ua_hash FROM link_views WHERE link_id = ?", (link_id,)).fetchone()
    assert len(row["ip_hash"]) == 64
    assert len(row["ua_hash"]) == 64
    assert "127.0.0.1" not in str(dict(row))


async def test_second_get_does_not_change_first_viewed_at(authed_client, client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    await client.get(f"/v/{token}")
    from broadcaster.db import get_db
    with get_db() as conn:
        first = conn.execute("SELECT first_viewed_at FROM broadcast_links WHERE id = ?", (link_id,)).fetchone()["first_viewed_at"]
    await client.get(f"/v/{token}")
    with get_db() as conn:
        second = conn.execute("SELECT first_viewed_at FROM broadcast_links WHERE id = ?", (link_id,)).fetchone()["first_viewed_at"]
    assert first == second


# ── POST /view (idempotent) ──────────────────────────────────

async def test_post_view_returns_first_flag(authed_client, client):
    token, _, _ = await _make_user_and_broadcast(authed_client)
    r1 = await client.post(f"/v/{token}/view")
    assert r1.status_code == 200
    body = r1.json()
    assert body["ok"] is True
    assert body["first_view"] is True
    r2 = await client.post(f"/v/{token}/view")
    assert r2.json()["first_view"] is False


async def test_post_view_unknown_token_returns_410(client):
    r = await client.post("/v/totally-fake-token/view")
    assert r.status_code == 410


# ── /v/{token}/media ─────────────────────────────────────────

async def test_media_endpoint_streams_content(authed_client, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = {"file": ("x.bin", io.BytesIO(b"media-bytes"), "application/octet-stream")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]
    token, _, _ = await _make_user_and_broadcast(authed_client, content_id=cid)
    r = await client.get(f"/v/{token}/media")
    assert r.status_code == 200
    assert r.content == b"media-bytes"


async def test_media_endpoint_404_when_no_content(authed_client, client):
    token, _, _ = await _make_user_and_broadcast(authed_client)
    r = await client.get(f"/v/{token}/media")
    assert r.status_code == 404


async def test_media_endpoint_410_for_expired_token(authed_client):
    token, link_id, _ = await _make_user_and_broadcast(authed_client)
    from broadcaster.db import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE broadcast_links SET expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (link_id,),
        )
    r = await authed_client.get(f"/v/{token}/media")
    assert r.status_code == 410


# ── No auth required for viewer ──────────────────────────────

async def test_viewer_requires_no_auth(authed_client, client):
    token, _, _ = await _make_user_and_broadcast(authed_client)
    # No login first for the viewer GET
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200


# ── Graceful handling when the underlying file is gone ─────

async def test_viewer_skips_video_when_file_missing(authed_client, client, tmp_path, monkeypatch):
    """If the content row exists in DB but the underlying file vanished
    (volume reset, manual cleanup, etc.), the viewer must NOT render a
    broken <video> element. Instead it should show a 'Media unavailable'
    notice and the /media endpoint should return 404."""
    monkeypatch.chdir(tmp_path)
    files = {"file": ("gone.mp4", io.BytesIO(b"video-bytes"), "video/mp4")}
    cr = await authed_client.post("/api/content/media", files=files)
    cid = cr.json()["id"]
    token, _, _ = await _make_user_and_broadcast(authed_client, content_id=cid)

    # Remove the file but leave the content row intact.
    from broadcaster.db import get_db
    with get_db() as conn:
        path_row = conn.execute("SELECT content_data FROM content WHERE id = ?", (cid,)).fetchone()
    import os
    os.remove(path_row["content_data"])

    # Viewer page must NOT include <video> tag, MUST include the notice.
    r = await client.get(f"/v/{token}")
    assert r.status_code == 200
    assert "<video" not in r.text
    assert "Media unavailable" in r.text
    assert "media-missing" in r.text

    # /media endpoint still returns 404 (unchanged).
    r2 = await client.get(f"/v/{token}/media")
    assert r2.status_code == 404
