"""EmailProvider interface + value types (Phase 1 task P1.2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, runtime_checkable

__all__ = ["OutboundEmail", "SendResult", "EmailProvider"]


@dataclass(frozen=True)
class OutboundEmail:
    to_addr: str
    from_addr: str
    from_name: str
    subject: str
    html_body: str
    text_body: str
    reply_to: str
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    accepted: bool
    detail: Optional[str] = None


@runtime_checkable
class EmailProvider(Protocol):
    name: str

    def send(self, email: OutboundEmail) -> SendResult:
        ...
