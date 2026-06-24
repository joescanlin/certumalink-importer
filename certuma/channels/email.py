"""Email channel (Phase 3 task P3.8) - wraps the proven deterministic SENDER unchanged.

The email path keeps every guarantee it had (full Gate, CAN-SPAM compliance, at-most-once, RFC 8058
List-Unsubscribe, open pixel); this is a thin Channel adapter so email and LinkedIn share one
interface. The caller supplies the rendered email + recipient + mailbox, exactly as before.
"""
from __future__ import annotations

from certuma.sender import send_one

from .provider import ChannelResult

__all__ = ["EmailChannel"]


class EmailChannel:
    name = "email"

    def __init__(self, provider):
        self._provider = provider

    def send(self, session, lead, *, rendered, to_email, mailbox, settings=None, when=None, **_) -> ChannelResult:
        outcome = send_one(session, lead, mailbox=mailbox, to_email=to_email, rendered=rendered,
                           provider=self._provider, settings=settings, when=when)
        return ChannelResult(
            sent=outcome.sent, channel="email", message_id=outcome.message_id,
            reason=(outcome.decision.reason_code if outcome.decision else ""))
