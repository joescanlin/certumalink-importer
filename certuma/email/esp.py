"""Cold-tolerant ESP provider (Phase 1 seam, wired in Phase 3 task P3.10).

A real HTTP send through a cold-outreach ESP, behind the EmailProvider interface, so flipping
CERTUMA_EMAIL_PROVIDER=esp (with CERTUMA_ESP_API_KEY + CERTUMA_ESP_BASE_URL) cuts over from Mailpit
with no other code change. The request shape is a generic ESP JSON; map it to a specific provider's
exact fields at integration. The opener is injectable so this is testable without a network, and an
unconfigured ESP fails loudly rather than silently no-opping a send. Credentials/domain are the
stakeholder-gated part; the code path is ready.
"""
from __future__ import annotations

import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .provider import OutboundEmail, SendResult

__all__ = ["EspProvider"]

_PATH = "/v1/messages"
_USER_AGENT = "certuma-reach-esp/0.1"


class EspProvider:
    name = "esp"

    def __init__(self, settings, *, opener: Optional[Callable] = None, timeout: int = 20):
        self.settings = settings
        self._opener = opener
        self._timeout = timeout

    def send(self, email: OutboundEmail) -> SendResult:
        if not self.settings.esp_api_key or not self.settings.esp_base_url:
            raise NotImplementedError(
                "cold ESP not configured: set CERTUMA_ESP_API_KEY + CERTUMA_ESP_BASE_URL "
                "(or CERTUMA_EMAIL_PROVIDER=mailpit for dev)")
        body = {
            "to": email.to_addr,
            "from": email.from_addr,
            "from_name": email.from_name,
            "subject": email.subject,
            "html": email.html_body,
            "text": email.text_body,
            "reply_to": email.reply_to,
            "headers": dict(email.headers or {}),
        }
        endpoint = f"{self.settings.esp_base_url.rstrip('/')}{_PATH}"
        request = Request(
            endpoint, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.settings.esp_api_key}",
                     "Content-Type": "application/json", "User-Agent": _USER_AGENT})
        opener = self._opener or urlopen
        try:
            with opener(request, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8") if hasattr(resp, "read") else ""
                parsed = json.loads(raw) if raw else {}
                status = int(getattr(resp, "status", 200))
                mid = (parsed.get("id") or parsed.get("message_id")
                       or (resp.headers.get("X-Message-Id", "") if hasattr(resp, "headers") else ""))
                ok = 200 <= status < 300
                return SendResult(provider_message_id=mid or "", accepted=ok,
                                  detail=None if ok else f"status {status}")
        except HTTPError as exc:
            return SendResult(provider_message_id="", accepted=False, detail=f"http {exc.code}")
        except (URLError, TimeoutError) as exc:
            return SendResult(provider_message_id="", accepted=False, detail=f"transport: {exc}")
