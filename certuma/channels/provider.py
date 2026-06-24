"""Channel interface + result type (Phase 3 task P3.8). Pure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

__all__ = ["ChannelResult", "Channel"]


@dataclass(frozen=True)
class ChannelResult:
    sent: bool
    channel: str
    message_id: Optional[int] = None
    reason: str = ""


class Channel(Protocol):
    name: str

    def send(self, session, lead, *, settings=None, when=None, **payload) -> ChannelResult:
        ...
