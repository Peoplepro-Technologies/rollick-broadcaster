"""Forgot-password / recovery flow tests.

Covers:
- Service unit: `password_reset.request_reset` returns the right
  `(ok, detail)` codes for each failure mode and the happy path.
- Service unit: `generate_strong_password` has the right length and
  alphabet (no ambiguous chars, only [A-Za-z0-9]).
- Schema: `init_db` seeds `password_recovery_email` with the default
  and preserves operator edits across re-runs.
- Integration: `POST /api/auth/forgot-password` round-trip.
- Integration: forced-change redirect — after a reset, signing in
  redirects to /admin/change-password and every protected route 303s
  there until the flag is cleared.
- Integration: `/api/auth/change-password` rejects bad old / mismatched
  confirm / short new.
- Integration: settings page shows + saves the recovery mailbox field
  and the Test-recovery-mailbox endpoint pings the configured address.
"""
from __future__ import annotations

import re

import pytest

from broadcaster.security import generate_strong_password, verify_password
from broadcaster.services import admin as admin_svc
from broadcaster.services import password_reset as reset_svc
from broadcaster.services import settings as settings_svc
from broadcaster.db import DEFAULT_PASSWORD_RECOVERY_EMAIL, init_db
from broadcaster.settings import bust_settings_cache
from broadcaster.services.senders import SendResult


# ── helpers ─────────────────────────────────────────────────────

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


def _patch_email_send(monkeypatch, ok: bool = True, error: str = ""):
    """Patch `EmailSender.send` to capture the message and return a
    canned `SendResult`. Returns the `sent` dict tests can inspect."""
    sent: dict = {"calls": 0}

    def fake_send(self, message):
        sent["calls"] += 1
        sent["to"] = message.recipient
        sent["subject"] = message.subject
        sent["body"] = message.body
        if ok:
            return SendResult(ok=True, provider_id=message.recipient)
        return SendResult(ok=False, error=error or "smtp: simulated outage")

    monkeypatch.setattr(
        "broadcaster.services.email.EmailSender.send", fake_send)
    return sent


def _extract_temp_pwd(body: str) -> str:
    """Pull the temp password out of the email body.

    The body template places the password on its own line between two
    blank lines, preceded by "A temporary password has been generated:".
    The regex tolerates any whitespace / newlines between the label and
    the value so changes to the surrounding format don't break the
    extractor."""
    m = re.search(r"A temporary password has been generated:\s*\n\s*(\S+)", body)
    assert m, f"temp pwd not found in body:\n{body}"
    return m.group(1)


# ── generate_strong_password ────────────────────────────────────

def test_generate_strong_password_default_length():
    p = generate_strong_password()
    assert len(p) == 14
    for ch in p:
        assert ch.isalnum(), f"unexpected non-alnum char {ch!r}"


def test_generate_strong_password_no_ambiguous_chars():
    for _ in range(100):
        p = generate_strong_password()
        for ch in "0O1lI":
            assert ch not in p, f"ambiguous char {ch!r} in {p!r}"


def test_generate_strong_password_two_calls_differ():
    assert generate_strong_password() != generate_strong_password()


# ── request_reset service unit ──────────────────────────────────

def test_request_reset_unknown_user_returns_no_such_admin():
    ok, detail = reset_svc.request_reset("ghost")
    assert (ok, detail) == (False, "no_such_admin")


def test_request_reset_no_recovery_mailbox_returns_config_error(
        monkeypatch, recovery_settings):
    settings_svc.set_("password_recovery_email", "")
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (False, "recovery_mailbox_not_configured")


def test_request_reset_no_smtp_returns_smtp_not_configured(monkeypatch):
    settings_svc.set_("password_recovery_email", "it-test@rollick.co.in")
    bust_settings_cache()
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (False, "smtp_not_configured")


def test_request_reset_happy_path_mints_password_and_sets_flag(
        monkeypatch, recovery_settings):
    """Happy path: temp pwd generated, hashed, must_change set, email sent."""
    sent = _patch_email_send(monkeypatch, ok=True)
    row_before = admin_svc.find_by_username("admin")
    assert row_before["must_change_password"] == 0

    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (True, "sent")
    assert sent["to"] == "it-test@rollick.co.in"
    assert "admin" in sent["subject"]
    assert "A password recovery request was received" in sent["body"]
    assert "Username: admin" in sent["body"]
    assert "Regards,\nSupport Team" in sent["body"]

    row_after = admin_svc.find_by_username("admin")
    assert row_after["must_change_password"] == 1
    assert row_after["password_hash"] != row_before["password_hash"]

    temp_pwd = _extract_temp_pwd(sent["body"])
    assert verify_password(temp_pwd, row_after["password_hash"])
    assert not verify_password("test-admin-pass", row_after["password_hash"])


def test_request_reset_smtp_failure_rolls_back_password(monkeypatch, recovery_settings):
    sent = _patch_email_send(monkeypatch, ok=False, error="smtp: simulated outage")
    row_before = admin_svc.find_by_username("admin")
    hash_before = row_before["password_hash"]

    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (False, "send_failed")
    row_after = admin_svc.find_by_username("admin")
    # Flag cleared (admin isn't stuck behind a change-password screen).
    assert row_after["must_change_password"] == 0
    # Password rotated to a fresh random one (the original plaintext is
    # unknown — best we can do is rotate forward and clear the flag).
    assert row_after["password_hash"] != hash_before
    assert sent["calls"] == 1


# ── Recipient resolution (per-admin email + global fallback) ────

def test_request_reset_uses_admin_recovery_email(
        monkeypatch, recovery_settings):
    """When the admin row carries a recovery_email and the global
    setting is empty, the temp password routes to the per-admin row."""
    settings_svc.set_("password_recovery_email", "")
    bust_settings_cache()
    admin_id = admin_svc.find_by_username("admin")["id"]
    admin_svc.set_recovery_email(admin_id, "alice@rollick.co.in")

    sent = _patch_email_send(monkeypatch, ok=True)
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (True, "sent")
    assert sent["to"] == "alice@rollick.co.in"


def test_request_reset_prefers_admin_over_global(
        monkeypatch, recovery_settings):
    """When both the per-admin row and the global setting are set,
    the per-admin row wins — IT is no longer in the loop."""
    # recovery_settings already seeded the global; the bootstrap admin's
    # row has recovery_email='' (DB default). Add a per-admin entry to
    # force the preferred path.
    admin_id = admin_svc.find_by_username("admin")["id"]
    admin_svc.set_recovery_email(admin_id, "personal@rollick.co.in")

    sent = _patch_email_send(monkeypatch, ok=True)
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (True, "sent")
    assert sent["to"] == "personal@rollick.co.in"


def test_request_reset_falls_back_to_global_when_admin_empty(
        monkeypatch, recovery_settings):
    """When the admin row's recovery_email is empty (legacy backfill,
    or a row a super_admin hasn't filled in yet), the global setting
    is used. The bootstrap admin's row has recovery_email='' by default,
    which is the setup for this test."""
    # recovery_settings seeds the global; admin row is empty.
    assert admin_svc.find_by_username("admin")["recovery_email"] == ""
    sent = _patch_email_send(monkeypatch, ok=True)
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (True, "sent")
    assert sent["to"] == "it-test@rollick.co.in"


def test_request_reset_no_destinations_returns_config_error(monkeypatch):
    """When neither the per-admin row nor the global setting has a
    value, the service returns the existing
    `recovery_mailbox_not_configured` detail code (no new code needed —
    the contract is unchanged for this failure mode)."""
    # Both empty. The autouse fixture zeros SMTP env too; the
    # mailbox-config error wins because we check it before SMTP.
    settings_svc.set_("password_recovery_email", "")
    bust_settings_cache()
    # Admin row already has recovery_email='' (DEFAULT).
    assert admin_svc.find_by_username("admin")["recovery_email"] == ""
    ok, detail = reset_svc.request_reset("admin")
    assert (ok, detail) == (False, "recovery_mailbox_not_configured")


# ── Default seed ────────────────────────────────────────────────

def test_init_db_seeds_default_recovery_mailbox(test_db_path, monkeypatch):
    """Fresh DB on disk → settings has the default recovery mailbox."""
    fresh = test_db_path.parent / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", str(fresh))
    bust_settings_cache()
    init_db()
    assert settings_svc.get("password_recovery_email") == DEFAULT_PASSWORD_RECOVERY_EMAIL


def test_init_db_preserves_operator_edits_across_runs(test_db_path, monkeypatch):
    """INSERT OR IGNORE means operator edits to the seed key survive a
    re-init (e.g. after schema migration)."""
    settings_svc.set_("password_recovery_email", "ops@custom.example")
    init_db()
    assert settings_svc.get("password_recovery_email") == "ops@custom.example"


# ── /api/auth/forgot-password integration ───────────────────────

async def test_get_forgot_password_page_renders(client):
    r = await client.get("/admin/forgot-password")
    assert r.status_code == 200
    assert "Forgot password" in r.text


async def test_post_forgot_password_happy_path_returns_200(
        client, monkeypatch, recovery_settings):
    _patch_email_send(monkeypatch, ok=True)
    r = await client.post("/api/auth/forgot-password", json={"username": "admin"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "detail": "sent"}


async def test_post_forgot_password_unknown_user_returns_400(client):
    r = await client.post("/api/auth/forgot-password", json={"username": "ghost"})
    assert r.status_code == 400
    assert r.json()["detail"] == "no_such_admin"


async def test_post_forgot_password_missing_username_returns_400(client):
    r = await client.post("/api/auth/forgot-password", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "username_required"


async def test_post_forgot_password_existing_user_without_config_returns_400(
        client, recovery_settings):
    # Recovery mailbox cleared → config error wins.
    settings_svc.set_("password_recovery_email", "")
    r = await client.post("/api/auth/forgot-password", json={"username": "admin"})
    assert r.status_code == 400
    assert r.json()["detail"] in (
        "recovery_mailbox_not_configured",
        "smtp_not_configured",
    )


# ── Forced-change redirect ──────────────────────────────────────

async def test_login_with_temp_password_redirects_to_change_page(
        client, monkeypatch, recovery_settings):
    sent = _patch_email_send(monkeypatch, ok=True)
    reset_svc.request_reset("admin")
    temp = _extract_temp_pwd(sent["body"])

    r = await _login(client, password=temp)
    assert r.status_code == 200
    body = r.json()
    assert body["must_change_password"] is True
    assert body["redirect"] == "/admin/change-password"


async def test_protected_page_303s_to_change_when_must_change(
        client, monkeypatch, recovery_settings):
    sent = _patch_email_send(monkeypatch, ok=True)
    reset_svc.request_reset("admin")
    temp = _extract_temp_pwd(sent["body"])
    await _login(client, password=temp)

    r = await client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/change-password"


async def test_change_password_page_renders_without_admin_nav(
        client, monkeypatch, recovery_settings):
    """The change page must work without going through _page_admin —
    it has no admin-nav block."""
    sent = _patch_email_send(monkeypatch, ok=True)
    reset_svc.request_reset("admin")
    temp = _extract_temp_pwd(sent["body"])
    await _login(client, password=temp)

    r = await client.get("/admin/change-password")
    assert r.status_code == 200
    assert "Change your password" in r.text


async def test_change_password_clears_flag_and_allows_access(
        client, monkeypatch, recovery_settings):
    sent = _patch_email_send(monkeypatch, ok=True)
    reset_svc.request_reset("admin")
    temp = _extract_temp_pwd(sent["body"])
    await _login(client, password=temp)

    r = await client.post("/api/auth/change-password", json={
        "old_password": temp,
        "new_password": "MyNewPass!1",
        "confirm":      "MyNewPass!1",
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    row = admin_svc.find_by_username("admin")
    assert row["must_change_password"] == 0

    # Protected page now reachable.
    r2 = await client.get("/admin/users", follow_redirects=False)
    assert r2.status_code == 200


async def test_change_password_rejects_wrong_old(client, authed_client):
    r = await authed_client.post("/api/auth/change-password", json={
        "old_password": "wrong",
        "new_password": "MyNewPass!1",
        "confirm":      "MyNewPass!1",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "wrong_old_password"


async def test_change_password_rejects_mismatched_confirm(client, authed_client):
    r = await authed_client.post("/api/auth/change-password", json={
        "old_password": "test-admin-pass",
        "new_password": "MyNewPass!1",
        "confirm":      "MyNewPass!2",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "confirm_mismatch"


async def test_change_password_rejects_short_new(client, authed_client):
    r = await authed_client.post("/api/auth/change-password", json={
        "old_password": "test-admin-pass",
        "new_password": "short",
        "confirm":      "short",
    })
    assert r.status_code == 400
    assert r.json()["detail"] == "password_too_short"


async def test_change_password_requires_session(client):
    r = await client.post("/api/auth/change-password", json={
        "old_password": "x", "new_password": "yyyyyyyy", "confirm": "yyyyyyyy"})
    assert r.status_code == 401


# ── Settings page ───────────────────────────────────────────────

async def test_settings_page_shows_recovery_mailbox_field(authed_client):
    r = await authed_client.get("/admin/settings")
    assert r.status_code == 200
    assert "password_recovery_email" in r.text
    assert DEFAULT_PASSWORD_RECOVERY_EMAIL in r.text


async def test_settings_saves_recovery_mailbox(authed_client):
    r = await authed_client.post("/api/settings", json={
        "password_recovery_email": "new-mailbox@rollick.co.in",
    })
    assert r.status_code == 200
    assert r.json()["saved"] == 1
    assert settings_svc.get("password_recovery_email") == "new-mailbox@rollick.co.in"


async def test_test_recovery_mailbox_button_sends_ping(
        authed_client, monkeypatch, recovery_settings):
    sent = _patch_email_send(monkeypatch, ok=True)
    r = await authed_client.post("/api/settings/test-recovery-mailbox")
    assert r.status_code == 200
    assert sent["to"] == "it-test@rollick.co.in"


async def test_test_recovery_mailbox_without_config_returns_400(authed_client):
    """With SMTP_HOST forced empty by the autouse fixture and no
    recovery mailbox, the SMTP check fires first (it always has, in
    parity with /api/settings/test-smtp). The endpoint is only useful
    when SMTP is set up — config errors come back as 400 either way.
    """
    settings_svc.set_("password_recovery_email", "")
    r = await authed_client.post("/api/settings/test-recovery-mailbox")
    assert r.status_code == 400
    assert r.json()["detail"] in {
        "recovery_mailbox_not_configured",
        "smtp_not_configured",
    }
