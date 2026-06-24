"""Multi-channel outreach (Phase 3 task P3.8).

One Channel interface for outreach so the system is no longer email-hardcoded: EmailChannel wraps the
proven SENDER unchanged (full Gate + CAN-SPAM + at-most-once), StubLinkedInChannel adds a second
channel behind the seam (suppression-honoring, idempotent, API stubbed). message.channel records which
channel a touch used, so the analytics funnel can break down by channel and a campaign can sequence
across channels. A real LinkedIn connector slots in behind the same interface later.
"""
from .provider import Channel, ChannelResult
from .email import EmailChannel
from .linkedin import StubLinkedInChannel

__all__ = ["Channel", "ChannelResult", "EmailChannel", "StubLinkedInChannel", "CHANNELS", "get_channel"]

CHANNELS = ("email", "linkedin")


def get_channel(name: str, *, email_provider=None) -> Channel:
    """Resolve a channel by name. The email channel needs an EmailProvider; linkedin is self-contained."""
    if name == "email":
        if email_provider is None:
            raise ValueError("the email channel requires an email_provider")
        return EmailChannel(email_provider)
    if name == "linkedin":
        return StubLinkedInChannel()
    raise ValueError(f"unknown channel {name!r}")
