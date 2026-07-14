"""AiSensy WhatsApp sender (httpx, campaign API v2).

Used as the preferred WhatsApp provider when `aisensy_api_key` is set
in the environment. Falls back to `WhatsAppSender` (direct Meta Cloud
API) and then `MockSender` when AiSensy is unconfigured — see
`get_sender_for("whatsapp")` in `senders.py`.

AiSensy's standard campaign API request body:
    {
      "apiKey": "<api_key>",
      "campaignName": "<template_name>",
      "destination": "<E.164 phone>",
      "userName": "<optional, used in template header>",
      "templateParams": ["<param1>", "<param2>", ...],
      "source": "Rollick Broadcaster",
      "media": {"url": "...", "filename": "..."},
      "buttons": [{"type": "URL", "url": "..."}]
    }

Response shape (200):
    {"status": "success", "msgId": "<id>", ...}
or on error:
    {"status": "error", "message": "..."}
"""
from __future__ import annotations

import httpx

from broadcaster.services.senders import Message, SendResult
from broadcaster.settings import get_settings


DEFAULT_BASE_URL = "https://backend.aisensy.com/campaign/t1/api/v2"
DEFAULT_SOURCE = "Rollick Broadcaster"


class AiSensySender:
    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.aisensy_api_key
        self.campaign_name = s.aisensy_campaign_name
        self.base_url = (s.aisensy_base_url or DEFAULT_BASE_URL).rstrip("/")
        self.country_code = s.whatsapp_country_code
        self._client = httpx.Client(timeout=15.0)

    def _normalize_phone(self, phone: str) -> str:
        """Match WhatsAppSender._normalize_phone — Indian 10-digit → E.164.

        AiSensy accepts the same number format as Meta's Cloud API.
        """
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith(self.country_code):
            return digits
        if digits.startswith("0"):
            digits = digits.lstrip("0")
        return self.country_code + digits

    def _payload(self, message: Message) -> dict:
        return {
            "apiKey": self.api_key,
            "campaignName": self.campaign_name,
            "destination": self._normalize_phone(message.recipient),
            "templateParams": [message.body],
            "source": DEFAULT_SOURCE,
        }

    def send(self, message: Message) -> SendResult:
        try:
            r = self._client.post(
                self.base_url,
                json=self._payload(message),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if r.status_code >= 300:
                return SendResult(
                    ok=False,
                    error=f"http {r.status_code}: {r.text[:200]}",
                )
            data = r.json()
            # AiSensy returns status: "success" / "error"
            if isinstance(data, dict) and data.get("status") == "error":
                return SendResult(
                    ok=False,
                    error=f"aisensy: {data.get('message', 'unknown error')[:200]}",
                )
            msg_id = (
                data.get("msgId")
                or data.get("messageId")
                or data.get("id")
            )
            return SendResult(ok=True, provider_id=msg_id)
        except Exception as e:
            return SendResult(ok=False, error=f"exception: {e}")
