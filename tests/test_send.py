"""Phase 4 — Send fan-out via MockSender (no real creds needed)."""
from __future__ import annotations

import json
import os
from pathlib import Path

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


async def _setup_broadcast(client, *, delivery_channel="whatsapp", count=2,
                            has_email=True, message_text=None):
    """Create N users + 1 broadcast + return (bid, link_tokens, user_ids)."""
    user_ids = []
    for i in range(count):
        u = (await client.post("/api/users", json={
            "name": f"User{i}",
            "phone": f"90000000{i:02d}",
            **({"email": f"u{i}@x.com"} if has_email else {}),
        })).json()
        user_ids.append(u["id"])
    b = (await client.post("/api/broadcasts", json={
        "title": "Hello",
        "delivery_channel": delivery_channel,
        "user_ids": user_ids,
        **({"message_text": message_text} if message_text else {}),
    })).json()
    bid = b["id"]
    links = (await client.get(f"/api/broadcasts/{bid}/links")).json()
    tokens = [l["token"] for l in links]
    return bid, tokens, user_ids


# ── Mock send writes files to sent_log/ ──────────────────────

async def test_whatsapp_send_writes_to_sent_log(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, delivery_channel="whatsapp")
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sent"
    assert body["counters"]["whatsapp"]["sent"] == 2
    assert body["counters"]["whatsapp"]["failed"] == 0

    log_dir = tmp_path / "sent_log" / "whatsapp"
    files = list(log_dir.glob("*.json"))
    assert len(files) == 2
    payload = json.loads(files[0].read_text())
    assert payload["channel"] == "whatsapp"
    assert "/v/" in payload["viewer_link"]


async def test_email_send_writes_to_sent_log(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, delivery_channel="email", count=1)
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    body = r.json()
    assert body["status"] == "sent"
    assert body["counters"]["email"]["sent"] == 1
    log_dir = tmp_path / "sent_log" / "email"
    files = list(log_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["subject"] == "Hello"
    assert payload["recipient"] == "u0@x.com"


async def test_both_sends_to_both_channels(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, delivery_channel="both", count=1)
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    body = r.json()
    assert body["counters"]["whatsapp"]["sent"] == 1
    assert body["counters"]["email"]["sent"] == 1
    assert (tmp_path / "sent_log" / "whatsapp").exists()
    assert (tmp_path / "sent_log" / "email").exists()


# ── Status transitions ───────────────────────────────────────

async def test_send_marks_broadcast_sent(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, count=1)
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    assert r.json()["status"] == "sent"
    # Confirm DB state
    b = (await authed_client.get(f"/api/broadcasts/{bid}")).json()
    assert b["status"] == "sent"
    assert b["sent_at"] is not None
    assert b["whatsapp_status"] == "sent:1,failed:0"


async def test_cannot_send_twice(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, count=1)
    await authed_client.post(f"/api/broadcasts/{bid}/send")
    r2 = await authed_client.post(f"/api/broadcasts/{bid}/send")
    assert r2.status_code == 400
    assert r2.json()["detail"] == "cannot_send_sent_broadcast"


async def test_cannot_send_cancelled(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, count=1)
    await authed_client.post(f"/api/broadcasts/{bid}/cancel")
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot_send_cancelled_broadcast"


# ── Message rendering ────────────────────────────────────────

async def test_message_includes_viewer_link(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, count=1)
    await authed_client.post(f"/api/broadcasts/{bid}/send")
    files = list((tmp_path / "sent_log" / "whatsapp").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert "/v/" in payload["body"]
    assert payload["viewer_link"] in payload["body"]


async def test_message_uses_admin_placeholder(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(
        authed_client, count=1,
        message_text="Hi! Watch: {{viewer_link}} — bye",
    )
    await authed_client.post(f"/api/broadcasts/{bid}/send")
    files = list((tmp_path / "sent_log" / "whatsapp").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert "{{viewer_link}}" not in payload["body"]
    assert "/v/" in payload["body"]
    # Title is prepended if not already present
    assert payload["body"].startswith("Hello")


async def test_skips_users_missing_email_for_email_channel(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # count=1 with has_email=False
    bid, _, _ = await _setup_broadcast(authed_client, delivery_channel="email",
                                        count=1, has_email=False)
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    body = r.json()
    assert body["counters"]["email"]["failed"] == 1
    assert body["counters"]["email"]["sent"] == 0
    # No files written
    assert not (tmp_path / "sent_log" / "email").exists()


async def test_skips_revoked_links(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, _ = await _setup_broadcast(authed_client, count=2)
    links = (await authed_client.get(f"/api/broadcasts/{bid}/links")).json()
    # Revoke the first link
    await authed_client.post(f"/api/broadcasts/{bid}/links/{links[0]['id']}/revoke")
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    body = r.json()
    # Only 1 active link should be sent
    assert body["counters"]["whatsapp"]["sent"] == 1


async def test_inactive_users_excluded(authed_client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bid, _, user_ids = await _setup_broadcast(authed_client, count=2)
    # Deactivate one user
    await authed_client.patch(f"/api/users/{user_ids[0]}", json={"is_active": False})
    r = await authed_client.post(f"/api/broadcasts/{bid}/send")
    body = r.json()
    assert body["counters"]["whatsapp"]["sent"] == 1


# ── Auth ──────────────────────────────────────────────────────

async def test_send_requires_auth(client):
    r = await client.post("/api/broadcasts/1/send")
    assert r.status_code == 401
