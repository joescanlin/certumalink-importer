"""Email provider seam (Phase 1 task P1.2).

A provider-agnostic interface so MailpitProvider is the dev sender now and a cold-tolerant ESP
slots in later behind the same interface. build_outbound enforces the RFC 8058 one-click
List-Unsubscribe headers. Body-level compliance (postal address, unsubscribe link, claim_url) is
rendered and pre-linted upstream (the render step / certuma_core.linter); this layer does NOT
re-validate body content. The SENDER applies a cheap last-line presence guard before sending.
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
