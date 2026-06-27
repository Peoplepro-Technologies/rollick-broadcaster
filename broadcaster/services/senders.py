"""Sender interfaces + MockSender.

For v1 the default sender is `MockSender` — it writes what would have
been sent to `sent_log/{channel}/{timestamp}.json` so the admin can
inspect the actual payload without ever calling Meta/SMTP.

Real `WhatsAppSender` and `EmailSender` are wired and used automatically
when their env credentials are set (WHATSAPP_*, SMTP_*). Otherwise the
mock is used so the app stays runnable without secrets.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from broadcaster.settings import get_settings


SENT_LOG_DIR = "sent_log"


@dataclass
class Message:
    channel: str            # "whatsapp" | "email"
    recipient: str          # phone (with country code) or email
    subject: str | None     # email only
    body: str
    viewer_link: str        # the per-subscriber URL
    broadcast_id: int
    user_id: int
    link_id: int


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    provider_id: str | None = None  # e.g. WhatsApp message id, SMTP envelope id


class Sender(Protocol):
    def send(self, message: Message) -> SendResult: ...


# ── MockSender (default) ─────────────────────────────────────

class MockSender:
    """Writes each would-be send to `sent_log/{channel}/<ts>.json`.
    No external network. Inspectable on disk.
    """
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path.cwd()

    def send(self, message: Message) -> SendResult:
        out_dir = self.base_dir / SENT_LOG_DIR / message.channel
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S.%f")
        recipient_safe = "".join(c if c.isalnum() else "_" for c in message.recipient)
        path = out_dir / f"{ts}-{recipient_safe}.json"
        path.write_text(json.dumps({
            "channel": message.channel,
            "recipient": message.recipient,
            "subject": message.subject,
            "body": message.body,
            "viewer_link": message.viewer_link,
            "broadcast_id": message.broadcast_id,
            "user_id": message.user_id,
            "link_id": message.link_id,
        }, indent=2))
        return SendResult(ok=True, provider_id=f"mock-{path.name}")


# ── Resolution: real if creds, mock otherwise ────────────────

def _whatsapp_creds_set() -> bool:
    s = get_settings()
    return bool(s.whatsapp_phone_id and s.whatsapp_access_token)


def _email_creds_set() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_from)


# Lazy imports so the real senders don't need to be present if unused.
def get_sender_for(channel: str) -> Sender:
    if channel == "whatsapp" and _whatsapp_creds_set():
        from broadcaster.services.whatsapp import WhatsAppSender
        return WhatsAppSender()
    if channel == "email" and _email_creds_set():
        from broadcaster.services.email import EmailSender
        return EmailSender()
    return MockSender()


def channels_to_use(delivery_channel: str) -> list[str]:
    if delivery_channel == "both":
        return ["whatsapp", "email"]
    return [delivery_channel]
