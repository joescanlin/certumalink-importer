"""Copy provider interface + value types (Phase 1 task P1.8)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Tuple

from certuma_core.copy_schema import SeedFacts

__all__ = ["TokenAudit", "CopyOutput", "CopyProvider"]


@dataclass(frozen=True)
class TokenAudit:
    token: str
    source: str


@dataclass(frozen=True)
class CopyOutput:
    """A draft. Personalization tokens are filled; the three compliance tokens
    ({claim_url},{unsubscribe_url},{postal_address}) are left literal for deterministic render."""
    subject: str
    body: str
    plaintext: str
    variant_id: str
    merge_token_audit: Tuple[TokenAudit, ...] = ()


class CopyProvider(Protocol):
    name: str

    def draft(self, *, template_subject: str, template_body: str, facts: SeedFacts, model: str) -> CopyOutput:
        ...
