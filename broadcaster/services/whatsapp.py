"""Real WhatsApp Business API sender (httpx, v21.0 by default).

Only instantiated when env creds are set; otherwise MockSender is used.
`httpx` keeps the dep tree small and gives us async + connection pooling
for free. The phone-number-id + access-token + version are all in
settings; the country code is prepended to the recipient phone if not
already present.
"""
from __future__ import annotations

import httpx

from broadcaster.services.senders import Message, SendResult
from broadcaster.settings import get_settings


class WhatsAppSender:
    def __init__(self) -> None:
        s = get_settings()
        self.phone_id = s.whatsapp_phone_id
        self.access_token = s.whatsapp_access_token
        self.country_code = s.whatsapp_country_code
        self.api_version = s.whatsapp_api_version
        self._client = httpx.Client(timeout=15.0)

    def _url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_id}/messages"

    def _normalize_phone(self, phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith(self.country_code):
            return digits
        if digits.startswith("0"):
            digits = digits.lstrip("0")
        return self.country_code + digits

    def send(self, message: Message) -> SendResult:
        try:
            payload = {
                "messaging_product": "whatsapp",
                "to": self._normalize_phone(message.recipient),
                "type": "text",
                "text": {"body": message.body},
            }
            r = self._client.post(
                self._url(),
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
            )
            if r.status_code >= 300:
                return SendResult(ok=False, error=f"http {r.status_code}: {r.text[:200]}")
            data = r.json()
            msg_id = (data.get("messages") or [{}])[0].get("id")
            return SendResult(ok=True, provider_id=msg_id)
        except Exception as e:
            return SendResult(ok=False, error=f"exception: {e}")
