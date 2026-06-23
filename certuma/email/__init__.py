"""Email provider seam (Phase 1 task P1.2).

A provider-agnostic interface so MailpitProvider is the dev sender now and a cold-tolerant ESP
slots in later behind the same interface. build_outbound enforces the RFC 8058 one-click
List-Unsubscribe headers; the body-level compliance (postal address, unsubscribe link, claim_url)
is rendered upstream and validated by the linter.
"""
from .provider import EmailProvider, OutboundEmail, SendResult
from .message import build_outbound, to_mime
from .mailpit import MailpitProvider
from .esp import EspProvider
from .factory import get_provider

__all__ = [
    "EmailProvider", "OutboundEmail", "SendResult",
    "build_outbound", "to_mime", "MailpitProvider", "EspProvider", "get_provider",
]
