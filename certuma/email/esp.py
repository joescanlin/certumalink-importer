"""Cold-tolerant ESP provider stub (Phase 1 task P1.2, deferred infra).

The real cold-outreach provider is wired when the cold domain + ESP account land. Until then this
stub makes the not-yet-wired state explicit (it never silently no-ops a send).
"""
from __future__ import annotations

from .provider import OutboundEmail, SendResult

__all__ = ["EspProvider"]


class EspProvider:
    name = "esp"

    def __init__(self, settings):
        self.settings = settings

    def send(self, email: OutboundEmail) -> SendResult:
        raise NotImplementedError(
            "cold-tolerant ESP provider not wired yet (deferred infra): set CERTUMA_EMAIL_PROVIDER=mailpit for dev"
        )
