"""Admin settings router — non-secret prefs + SMTP/WhatsApp test buttons."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from broadcaster.routes.admin_auth import require_admin
from broadcaster.services import settings as settings_svc

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
def get_settings():
    """Return all settings from the DB. Secrets (SMTP_*, WHATSAPP_*) are
    never read from the DB — they come from env. The frontend uses
    /api/auth/me + the /test-* endpoints to verify what's configured.
    """
    return settings_svc.all_visible()


@router.post("")
def update_settings(payload: dict):
    """Upsert a batch of keys. Rejects any key that looks like a secret."""
    forbidden = {"smtp_pass", "whatsapp_access_token", "whatsapp_app_secret",
                 "session_secret", "ip_hash_pepper", "media_sign_secret"}
    saved = 0
    rejected = []
    for k, v in payload.items():
        if k in forbidden:
            rejected.append(k)
            continue
        settings_svc.set_(k, str(v) if v is not None else "")
        saved += 1
    return {"saved": saved, "rejected": rejected}


@router.post("/test-smtp")
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


@router.post("/test-whatsapp")
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
