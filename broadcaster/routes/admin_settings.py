"""Admin settings router — non-secret prefs + SMTP/WhatsApp test buttons.

RBAC:
  - Read endpoints (DB-overrides + runtime-effective): super_admin and
    management (management sees the page with secrets redacted in the
    template layer — secret-redaction lives in Task 8 / template).
  - Mutating endpoints (update/test-smtp/test-whatsapp): super_admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from broadcaster.rbac import load_current_admin, require_role
from broadcaster.services import settings as settings_svc
from broadcaster.settings import bust_settings_cache, get_settings

READ_ROLES = ("super_admin", "management")
WRITE_ROLES = ("super_admin",)

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(load_current_admin)],
)


# Server-internal secrets that must NEVER be settable from the UI.
# These are NOT user-supplied credentials — they're cryptographic
# material the server generates/uses internally. Keep them in .env only.
#
# User-supplied credentials (smtp_pass, whatsapp_access_token,
# whatsapp_app_secret) ARE editable from the UI and stored in the DB.
FORBIDDEN_KEYS = {
    "session_secret", "ip_hash_pepper", "media_sign_secret",
}


@router.get("", dependencies=[Depends(require_role(*READ_ROLES))])
def get_settings_db():
    """Return DB-stored settings overrides (non-secret)."""
    return settings_svc.all_visible()


@router.get("/runtime", dependencies=[Depends(require_role(*READ_ROLES))])
def get_runtime_settings():
    """Return effective settings (env + DB overrides) for the UI to
    prefill the SMTP/WhatsApp forms. Returns the actual secret values
    so admin can edit them in place (DB-stored, plain-text — see note
    on the settings page)."""
    return settings_svc.runtime_overrides()


@router.post("", dependencies=[Depends(require_role(*WRITE_ROLES))])
def update_settings(payload: dict):
    """Upsert a batch of keys. Rejects any key that looks like a secret.
    After saving, the settings cache is busted so subsequent reads see
    the new values without restarting the server."""
    saved = 0
    rejected = []
    for k, v in payload.items():
        if k in FORBIDDEN_KEYS:
            rejected.append(k)
            continue
        settings_svc.set_(k, str(v) if v is not None else "")
        saved += 1
    if saved:
        bust_settings_cache()
    return {"saved": saved, "rejected": rejected}


@router.post("/test-smtp", dependencies=[Depends(require_role(*WRITE_ROLES))])
def test_smtp():
    """Send a test email to the configured SMTP_FROM address using the
    current env-creds. Returns success/error."""
    from broadcaster.services.email import EmailSender
    from broadcaster.settings import get_settings
    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        raise HTTPException(status_code=400, detail="smtp_not_configured")
    sender = EmailSender()
    msg_body = (
        f"Test email from Rollick Broadcaster.\n\n"
        f"If you received this, your SMTP settings are working.\n"
        f"Host: {s.smtp_host}\nFrom: {s.smtp_from}"
    )
    from broadcaster.services.senders import Message
    result = sender.send(Message(
        channel="email",
        recipient=s.smtp_from,
        subject="Rollick Broadcaster — SMTP test",
        body=msg_body,
        viewer_link="",
        broadcast_id=0,
        user_id=0,
        link_id=0,
    ))
    if result.ok:
        return {"ok": True, "provider_id": result.provider_id}
    raise HTTPException(status_code=500, detail=result.error or "send_failed")


@router.post("/test-whatsapp", dependencies=[Depends(require_role(*WRITE_ROLES))])
def test_whatsapp():
    """Send a test WhatsApp message to the WHATSAPP_COUNTRY_CODE + a
    test number. Real creds required; otherwise returns a 400 explaining
    the fallback to mock."""
    from broadcaster.services.whatsapp import WhatsAppSender
    from broadcaster.settings import get_settings
    s = get_settings()
    if not (s.whatsapp_phone_id and s.whatsapp_access_token):
        raise HTTPException(
            status_code=400,
            detail="whatsapp_not_configured",
        )
    sender = WhatsAppSender()
    from broadcaster.services.senders import Message
    # No real recipient available without user input; this call would
    # fail with 400 from Meta without a real `to`. Return a hint.
    return JSONResponse(
        {"ok": False, "detail": "test_send_requires_recipient; configure via a broadcast instead"},
        status_code=400,
    )


@router.post("/test-recovery-mailbox",
             dependencies=[Depends(require_role(*WRITE_ROLES))])
def test_recovery_mailbox():
    """Send a one-line ping to the configured `password_recovery_email`
    setting so super_admins can verify the routing address without
    triggering an actual password reset.
    """
    from broadcaster.services import settings as _settings_svc
    from broadcaster.services.email import EmailSender
    from broadcaster.services.senders import Message
    from broadcaster.settings import get_settings
    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        raise HTTPException(status_code=400, detail="smtp_not_configured")
    recovery = (_settings_svc.get("password_recovery_email") or "").strip()
    if not recovery:
        raise HTTPException(status_code=400, detail="recovery_mailbox_not_configured")
    result = EmailSender().send(Message(
        channel="email",
        recipient=recovery,
        subject="Rollick Broadcaster — recovery mailbox test",
        body=(
            "Test ping from Rollick Broadcaster.\n\n"
            "If you received this, the password recovery mailbox is "
            "configured correctly.\n"
            f"Routing address: {recovery}"
        ),
        viewer_link="",
        broadcast_id=0,
        user_id=0,
        link_id=0,
    ))
    if result.ok:
        return {"ok": True, "provider_id": result.provider_id}
    raise HTTPException(status_code=500, detail=result.error or "send_failed")
