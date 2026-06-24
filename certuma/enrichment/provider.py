"""Enrichment provider interfaces + value types (Phase 2 task P2.5). Pure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol

__all__ = ["EmailCandidate", "VerifyResult", "EnrichProvider", "VerifyProvider", "VALID"]

VALID = "valid"  # the only verification status we will send to


@dataclass(frozen=True)
class EmailCandidate:
    email: str
    source: str
    is_role: bool = False     # info@/office@/... - demoted in favor of a personal mailbox


@dataclass(frozen=True)
class VerifyResult:
    status: str               # valid | risky | catch_all | invalid | unknown
    verifier: str = ""


class EnrichProvider(Protocol):
    name: str

    def discover(self, *, first_name: str, last_name: str, display_name: str, npi: str) -> List[EmailCandidate]:
        ...


class VerifyProvider(Protocol):
    name: str

    def verify(self, email: str) -> VerifyResult:
        ...
