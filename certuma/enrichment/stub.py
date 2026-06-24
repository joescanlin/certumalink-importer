"""Deterministic enrichment stubs (Phase 2 task P2.5) - no network.

StubEnrichProvider derives candidate addresses from the prospect's name; StubVerifyProvider scores
them by simple pattern so the waterfall is fully testable: a personal mailbox verifies VALID, a role
address verifies catch_all (so it is never sent to), and an obviously bad address verifies invalid.
"""
from __future__ import annotations

from typing import List

from .provider import EmailCandidate, VerifyResult

__all__ = ["StubEnrichProvider", "StubVerifyProvider", "ROLE_LOCALPARTS"]

ROLE_LOCALPARTS = frozenset({"info", "office", "admin", "contact", "hello", "team", "reception", "frontdesk"})


class StubEnrichProvider:
    name = "stub"

    def discover(self, *, first_name: str, last_name: str, display_name: str = "", npi: str = "") -> List[EmailCandidate]:
        first = (first_name or "").lower().strip()
        last = (last_name or "").lower().strip()
        out: List[EmailCandidate] = []
        if first and last:
            out.append(EmailCandidate(f"{first}.{last}@example.com", "name_pattern", is_role=False))
            out.append(EmailCandidate(f"{first[0]}{last}@example.com", "name_pattern", is_role=False))
        out.append(EmailCandidate("info@example.com", "directory", is_role=True))
        return out


class StubVerifyProvider:
    name = "stub"

    def verify(self, email: str) -> VerifyResult:
        local = email.split("@", 1)[0].lower()
        if "invalid" in email.lower():
            return VerifyResult(status="invalid", verifier="stub")
        if local in ROLE_LOCALPARTS:
            return VerifyResult(status="catch_all", verifier="stub")  # role address: not deliverable-personal
        if "." in local or len(local) > 3:
            return VerifyResult(status="valid", verifier="stub")
        return VerifyResult(status="unknown", verifier="stub")
