"""MailpitProvider - the dev EmailProvider (Phase 1 task P1.2).

Sends via SMTP to Mailpit (the isolated certuma-mailpit on 11026, or any SMTP host). The transport
is injectable so unit tests assert the assembled message without a network/Mailpit.
"""
from __future__ import annotations

import smtplib
from typing import Callable, Optional

from .message import to_mime
from .provider import OutboundEmail, SendResult

__all__ = ["MailpitProvider"]


class MailpitProvider:
    name = "mailpit"

    def __init__(self, host: str, port: int, *, timeout: int = 10, transport: Optional[Callable] = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        # transport(mime_message) -> None ; defaults to a real SMTP send
        self._transport = transport

    def _smtp_send(self, mime) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.send_message(mime)

    def send(self, email: OutboundEmail) -> SendResult:
        mime = to_mime(email)
        message_id = mime["Message-ID"]
        (self._transport or self._smtp_send)(mime)
        return SendResult(provider_message_id=message_id, accepted=True)
