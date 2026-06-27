"""Real SMTP email sender.

SMTP_SSL on port 465; STARTTLS otherwise. Falls back to no-TLS only if
SMTP_USE_TLS=false is explicitly set (not exposed in v1 — secure by
default).
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from broadcaster.services.senders import Message, SendResult
from broadcaster.settings import get_settings


class EmailSender:
    def __init__(self) -> None:
        s = get_settings()
        self.host = s.smtp_host
        self.port = s.smtp_port
        self.user = s.smtp_user
        self.password = s.smtp_pass
        self.from_addr = s.smtp_from

    def send(self, message: Message) -> SendResult:
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = message.recipient
        if message.subject:
            msg["Subject"] = message.subject
        msg.set_content(message.body)

        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=15) as srv:
                    if self.user:
                        srv.login(self.user, self.password)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as srv:
                    srv.starttls()
                    if self.user:
                        srv.login(self.user, self.password)
                    srv.send_message(msg)
            return SendResult(ok=True, provider_id=msg["To"])
        except Exception as e:
            return SendResult(ok=False, error=f"smtp: {e}")
