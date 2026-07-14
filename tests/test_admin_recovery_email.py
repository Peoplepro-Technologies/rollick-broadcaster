"""Tests for per-admin `recovery_email` on /admin/admins.

Covers the new fields on `POST /api/admins` and the new
`POST /api/admins/{id}/recovery-email` subpath added in the
2026-07-14 per-admin password recovery flow. The forgot-password
service's recipient-resolution branches live in
`tests/test_password_reset.py` — this file only covers the admin-
management surface.
"""
from __future__ import annotations

import pytest

from broadcaster.services import admin as admin_svc
from broadcaster.services import settings as settings_svc


async def _login(client, username: str = "admin", password: str = "test-admin-pass"):
    return await client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )


@pytest.fixture
async def authed_client(client):
    await _login(client)
    return client


async def _create(client, **payload):
    return await client.post("/api/admins", json=payload)


# ── POST /api/admins — recovery_email required at create ──────────

async def test_create_admin_requires_recovery_email_returns_400(authed_client):
    """No `recovery_email` in the payload → 400 invalid_email.

    The route layer validates with services.users.validate_email
    (required=True), so missing or empty input is rejected BEFORE the
    service is called."""
    r = await _create(authed_client,
                      username="alice", password="test1234pass",
                      role="content_admin")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_email"


async def test_create_admin_rejects_empty_string_recovery_email(authed_client):
    """An explicit empty string is treated the same as missing."""
    r = await _create(authed_client,
                      username="alice", password="test1234pass",
                      role="content_admin", recovery_email="")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_email"


async def test_create_admin_rejects_invalid_format_recovery_email(authed_client):
    """Anything that doesn't match EMAIL_RE → 400 invalid_email."""
    for bad in ("not-an-email", "missing-at-sign.com", "@no-local-part.com"):
        r = await _create(authed_client,
                          username="alice", password="test1234pass",
                          role="content_admin", recovery_email=bad)
        assert r.status_code == 400, f"expected 400 for {bad!r}"
        assert r.json()["detail"] == "invalid_email", f"for {bad!r}"


async def test_create_admin_accepts_valid_recovery_email(authed_client):
    """A well-formed email passes validation; the row is persisted and
    returned with the normalised value."""
    r = await _create(authed_client,
                      username="alice", password="test1234pass",
                      role="content_admin",
                      recovery_email="alice@rollick.co.in")
    assert r.status_code == 200
    body = r.json()
    assert body["recovery_email"] == "alice@rollick.co.in"
    assert body["role"] == "content_admin"
    # The persisted row carries the same value.
    row = admin_svc.find_by_id(body["id"])
    assert row["recovery_email"] == "alice@rollick.co.in"


# ── POST /api/admins/{id}/recovery-email ──────────────────────────

async def test_update_recovery_email_via_subpath(authed_client):
    """Update an existing admin's recovery_email; verify response body
    and DB row reflect the new value."""
    r = await _create(authed_client,
                      username="bob", password="test1234pass",
                      role="hr_admin",
                      recovery_email="bob-orig@rollick.co.in")
    assert r.status_code == 200
    bob_id = r.json()["id"]

    r2 = await authed_client.post(
        f"/api/admins/{bob_id}/recovery-email",
        json={"recovery_email": "bob-new@rollick.co.in"},
    )
    assert r2.status_code == 200
    assert r2.json()["recovery_email"] == "bob-new@rollick.co.in"

    row = admin_svc.find_by_id(bob_id)
    assert row["recovery_email"] == "bob-new@rollick.co.in"


async def test_update_recovery_email_rejects_invalid_format(authed_client):
    """A bad email on the update endpoint also returns 400 invalid_email."""
    r = await _create(authed_client,
                      username="carol", password="test1234pass",
                      role="hr_admin",
                      recovery_email="carol@rollick.co.in")
    assert r.status_code == 200
    carol_id = r.json()["id"]

    r2 = await authed_client.post(
        f"/api/admins/{carol_id}/recovery-email",
        json={"recovery_email": "not-an-email"},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"] == "invalid_email"


async def test_update_recovery_email_unknown_admin_returns_404(authed_client):
    """Updating an admin row that doesn't exist → 404."""
    r = await authed_client.post(
        "/api/admins/99999/recovery-email",
        json={"recovery_email": "x@example.com"},
    )
    assert r.status_code == 404


async def test_update_recovery_email_requires_field(authed_client):
    """Missing `recovery_email` key → 400 invalid_email."""
    r = await _create(authed_client,
                      username="dave", password="test1234pass",
                      role="management",
                      recovery_email="dave@rollick.co.in")
    assert r.status_code == 200
    dave_id = r.json()["id"]

    r2 = await authed_client.post(
        f"/api/admins/{dave_id}/recovery-email",
        json={},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"] == "invalid_email"


# ── GET /api/admins — list contract ──────────────────────────────

async def test_list_admins_includes_recovery_email(authed_client):
    """The list endpoint returns `recovery_email` on every row so the
    admin-management UI can render the existing value (and the
    per-row modal can pre-populate it)."""
    # The bootstrap admin (added by conftest autouse) has
    # `recovery_email=''` from the migration DEFAULT.
    r = await authed_client.get("/api/admins")
    assert r.status_code == 200
    rows = r.json()
    assert rows, "bootstrap admin should be present"
    for row in rows:
        assert "recovery_email" in row, f"row missing recovery_email: {row}"
        # The bootstrap admin's value should be the empty string
        # (the migration's DEFAULT and the post-migration contract).
        assert row["recovery_email"] == "", (
            f"expected empty recovery_email on bootstrap admin, got {row['recovery_email']!r}"
        )


async def test_bootstrap_admin_has_empty_recovery_email_post_migration():
    """Pin the migration contract: legacy / bootstrap admin rows are
    backfilled to `''` so the global fallback kicks in for them until
    a super_admin fills in the per-admin field.

    This is a non-fixture test on the autouse-isolated DB; the
    bootstrap admin is created by the autouse fixture itself, so this
    also implicitly proves the admin row insert at
    services/admin.py:bootstrap_admin doesn't need to mention
    `recovery_email` (the column DEFAULT does the work)."""
    row = admin_svc.find_by_username("admin")
    assert row is not None
    assert row["recovery_email"] == ""


# ── POST /api/admins/{id}/send-recovery-email ─────────────────────

async def test_send_recovery_email_routes_to_per_admin_email(
        monkeypatch, recovery_settings, authed_client):
    """Happy path: admin row carries a personal email; the endpoint
    emails the temp password there and returns the chosen recipient
    so the UI can confirm where it landed."""
    from broadcaster.services import password_reset as reset_svc
    from broadcaster.services.senders import SendResult
    settings_svc.set_("password_recovery_email", "")
    bust_settings_cache()
    r = await _create(authed_client,
                      username="alice", password="test1234pass",
                      role="content_admin",
                      recovery_email="alice@rollick.co.in")
    assert r.status_code == 200
    aid = r.json()["id"]

    sent: dict = {"calls": 0}
    def fake_send(self, message):
        sent["calls"] += 1
        sent["to"] = message.recipient
        sent["body"] = message.body
        return SendResult(ok=True, provider_id=message.recipient)
    monkeypatch.setattr(
        "broadcaster.services.email.EmailSender.send", fake_send)

    r2 = await authed_client.post(f"/api/admins/{aid}/send-recovery-email")
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True
    assert body["username"] == "alice"
    assert body["recipient"] == "alice@rollick.co.in"
    # Email went to the per-admin address, not the global fallback.
    assert sent["to"] == "alice@rollick.co.in"
    assert sent["calls"] == 1
    # Admin is now flagged to change on next sign-in.
    row = admin_svc.find_by_id(aid)
    assert row["must_change_password"] == 1


async def test_send_recovery_email_falls_back_to_global(
        monkeypatch, recovery_settings, authed_client):
    """When the admin row has an empty recovery_email, the global
    setting is used and returned as `recipient`."""
    from broadcaster.services.senders import SendResult
    sent: dict = {"calls": 0}
    def fake_send(self, message):
        sent["calls"] += 1
        sent["to"] = message.recipient
        return SendResult(ok=True, provider_id=message.recipient)
    monkeypatch.setattr(
        "broadcaster.services.email.EmailSender.send", fake_send)
    # Bootstrap admin has recovery_email=''; recovery_settings seeds
    # the global. Send recovery mail via the new endpoint.
    bootstrap_id = admin_svc.find_by_username("admin")["id"]
    r = await authed_client.post(
        f"/api/admins/{bootstrap_id}/send-recovery-email")
    assert r.status_code == 200
    body = r.json()
    assert body["recipient"] == "it-test@rollick.co.in"
    assert sent["to"] == "it-test@rollick.co.in"
    assert body["username"] == "admin"


async def test_send_recovery_email_no_destinations_returns_400(authed_client):
    """Both per-admin and global empty → 400 recovery_mailbox_not_configured.
    No email is sent, no flag is flipped."""
    settings_svc.set_("password_recovery_email", "")
    bust_settings_cache()
    bootstrap_id = admin_svc.find_by_username("admin")["id"]
    r = await authed_client.post(
        f"/api/admins/{bootstrap_id}/send-recovery-email")
    assert r.status_code == 400
    assert r.json()["detail"] == "recovery_mailbox_not_configured"
    # The must_change_password flag must NOT have been flipped.
    row = admin_svc.find_by_username("admin")
    assert row["must_change_password"] == 0


async def test_send_recovery_email_unknown_admin_returns_404(authed_client):
    r = await authed_client.post("/api/admins/99999/send-recovery-email")
    assert r.status_code == 404
    assert r.json()["detail"] == "admin_not_found"


async def test_send_recovery_email_smtp_not_configured_returns_400(
        authed_client):
    """Recovery mailbox is set but SMTP isn't (autouse zeros SMTP_HOST/SMTP_FROM).
    Should return the existing `smtp_not_configured` detail code."""
    settings_svc.set_("password_recovery_email", "ops@rollick.co.in")
    bust_settings_cache()
    bootstrap_id = admin_svc.find_by_username("admin")["id"]
    r = await authed_client.post(
        f"/api/admins/{bootstrap_id}/send-recovery-email")
    assert r.status_code == 400
    assert r.json()["detail"] == "smtp_not_configured"


# ── Local helper (re-declared; same shape as test_password_reset.py) ──
def bust_settings_cache():
    from broadcaster.settings import bust_settings_cache as _b
    _b()
