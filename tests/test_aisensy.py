"""Tests for AiSensy WhatsApp sender — covers sender resolution,
phone normalization, and HTTP send paths (happy / 4xx / network error).

Uses `httpx.MockTransport` to intercept the outbound POST without
touching the network. No respx dependency required.
"""
from __future__ import annotations

import httpx
import pytest

from broadcaster.services.senders import (
    Message,
    MockSender,
    get_sender_for,
)
from broadcaster.services.aisensy import AiSensySender
from broadcaster.settings import bust_settings_cache, get_settings


# ── helpers ──────────────────────────────────────────────────

def _configure_aisensy(monkeypatch, *, api_key="test-key", campaign="test-campaign",
                       base_url="https://backend.aisensy.com/campaign/t1/api/v2"):
    monkeypatch.setenv("AISENSY_API_KEY", api_key)
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", campaign)
    monkeypatch.setenv("AISENSY_BASE_URL", base_url)
    bust_settings_cache()


def _msg(recipient: str = "9876543210", body: str = "Hi") -> Message:
    return Message(
        channel="whatsapp",
        recipient=recipient,
        subject=None,
        body=body,
        viewer_link="http://localhost:8123/v/abc",
        broadcast_id=1,
        user_id=1,
        link_id=1,
    )


# ── sender resolution ────────────────────────────────────────

def test_aisensy_sender_unconfigured_returns_mock(monkeypatch):
    """No AISENSY_API_KEY → `get_sender_for("whatsapp")` falls back to MockSender."""
    monkeypatch.setenv("AISENSY_API_KEY", "")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    bust_settings_cache()
    assert isinstance(get_sender_for("whatsapp"), MockSender)


def test_aisensy_sender_configured_returns_aisensy(monkeypatch):
    """AISENSY_API_KEY + AISENSY_CAMPAIGN_NAME set → AiSensySender wins."""
    _configure_aisensy(monkeypatch)
    sender = get_sender_for("whatsapp")
    assert isinstance(sender, AiSensySender)
    s = get_settings()
    assert sender.api_key == "test-key"
    assert sender.campaign_name == "test-campaign"
    assert sender.base_url == "https://backend.aisensy.com/campaign/t1/api/v2"


def test_aisensy_missing_campaign_falls_back(monkeypatch):
    """API key without campaign name → not enough to use AiSensy, falls through."""
    monkeypatch.setenv("AISENSY_API_KEY", "test-key")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
    bust_settings_cache()
    assert isinstance(get_sender_for("whatsapp"), MockSender)


# ── phone normalization ──────────────────────────────────────

def test_aisensy_normalize_phone_indian_10digit():
    s = get_settings()
    sender = AiSensySender.__new__(AiSensySender)
    sender.country_code = s.whatsapp_country_code
    assert sender._normalize_phone("9876543210") == "919876543210"


def test_aisensy_normalize_phone_strips_leading_zero():
    s = get_settings()
    sender = AiSensySender.__new__(AiSensySender)
    sender.country_code = s.whatsapp_country_code
    assert sender._normalize_phone("09876543210") == "919876543210"


def test_aisensy_normalize_phone_already_has_country_code():
    s = get_settings()
    sender = AiSensySender.__new__(AiSensySender)
    sender.country_code = s.whatsapp_country_code
    assert sender._normalize_phone("919876543210") == "919876543210"


# ── send: happy path ─────────────────────────────────────────

def test_aisensy_send_happy_path(monkeypatch):
    _configure_aisensy(monkeypatch)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": "success", "msgId": "abc123"})

    sender = AiSensySender()
    sender._client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sender.send(_msg(body="Hello there"))
    assert result.ok is True
    assert result.provider_id == "abc123"
    assert captured["url"] == "https://backend.aisensy.com/campaign/t1/api/v2"
    # Body should be JSON with apiKey, campaignName, destination, templateParams
    import json
    body = json.loads(captured["body"])
    assert body["apiKey"] == "test-key"
    assert body["campaignName"] == "test-campaign"
    assert body["destination"] == "919876543210"
    assert body["templateParams"] == ["Hello there"]


# ── send: HTTP error ─────────────────────────────────────────

def test_aisensy_send_http_error(monkeypatch):
    _configure_aisensy(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    sender = AiSensySender()
    sender._client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sender.send(_msg())
    assert result.ok is False
    assert "401" in (result.error or "")
    assert "Unauthorized" in (result.error or "")


def test_aisensy_send_aisensy_error_status(monkeypatch):
    """AiSensy returns 200 with status=error payload on validation issues."""
    _configure_aisensy(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "message": "Invalid campaign name"},
        )

    sender = AiSensySender()
    sender._client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sender.send(_msg())
    assert result.ok is False
    assert "Invalid campaign name" in (result.error or "")


# ── send: network exception ──────────────────────────────────

def test_aisensy_send_network_exception(monkeypatch):
    _configure_aisensy(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    sender = AiSensySender()
    sender._client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sender.send(_msg())
    assert result.ok is False
    assert "exception" in (result.error or "").lower()


# ── admin route: test-aisensy ─────────────────────────────────

import pytest as _pytest  # local alias to avoid shadowing


async def _login(client):
    await client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "test-admin-pass"},
        headers={"Accept": "application/json"},
    )


@_pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


async def test_test_aisensy_endpoint_unconfigured(authed_client, monkeypatch):
    monkeypatch.setenv("AISENSY_API_KEY", "")
    monkeypatch.setenv("AISENSY_CAMPAIGN_NAME", "")
    bust_settings_cache()
    r = await authed_client.post("/api/settings/test-aisensy")
    assert r.status_code == 400
    assert r.json()["detail"] == "aisensy_not_configured"


async def test_test_aisensy_endpoint_configured(authed_client, monkeypatch):
    _configure_aisensy(monkeypatch)
    r = await authed_client.post("/api/settings/test-aisensy")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "aisensy"
    assert body["campaign"] == "test-campaign"


async def test_test_aisensy_endpoint_requires_auth(client):
    r = await client.post("/api/settings/test-aisensy")
    assert r.status_code == 401
