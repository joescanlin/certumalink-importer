"""Deterministic signal stubs (Phase 3 task P3.3) - no network.

PublicSignalProvider derives the free/public signals from what we already hold (specialty, state,
group size) plus a deterministic public message-volume proxy and activity flag - these are wired
now. VendorSignalProvider stubs the gated/paid signals (EHR, panel size) so the seam exists and the
loop is testable; a real healthcare data vendor slots in behind the same interface later. Values are
derived from the npi (no randomness) so tests and the dev loop are deterministic.
"""
from __future__ import annotations

import hashlib
from typing import List

from .provider import (EHR, GROUP_SIZE, MESSAGE_BURDEN, PANEL_SIZE, PUBLIC_ACTIVITY, SPECIALTY_BOARD,
                       STATE_LICENSE, ClinicianFacts, Signal)

__all__ = ["PublicSignalProvider", "VendorSignalProvider", "EHR_SYSTEMS"]

EHR_SYSTEMS = ("Epic", "Cerner", "athenahealth", "eClinicalWorks", "NextGen")


def _hash_int(npi: str, salt: str) -> int:
    return int(hashlib.md5(f"{npi}:{salt}".encode()).hexdigest(), 16)


class PublicSignalProvider:
    """Free/public signals - wired now."""
    name = "public"

    def signals(self, facts: ClinicianFacts) -> List[Signal]:
        out: List[Signal] = []
        if facts.specialty:
            out.append(Signal(SPECIALTY_BOARD, value=facts.specialty, source=self.name))
        if facts.state:
            out.append(Signal(STATE_LICENSE, value=facts.state, source=self.name))
        out.append(Signal(GROUP_SIZE, numeric_value=float(facts.group_size or 0), source=self.name))
        out.append(Signal(MESSAGE_BURDEN, numeric_value=float(_hash_int(facts.npi, "burden") % 100),
                          confidence=0.6, source=self.name))
        activity = "active" if (_hash_int(facts.npi, "activity") % 3) else "low"
        out.append(Signal(PUBLIC_ACTIVITY, value=activity, confidence=0.6, source=self.name))
        return out


class VendorSignalProvider:
    """Gated/paid signals (EHR, panel size) - stubbed seam, real vendor later."""
    name = "vendor_stub"

    def signals(self, facts: ClinicianFacts) -> List[Signal]:
        ehr = EHR_SYSTEMS[_hash_int(facts.npi, "ehr") % len(EHR_SYSTEMS)]
        panel = 500 + (_hash_int(facts.npi, "panel") % 4000)
        return [
            Signal(EHR, value=ehr, confidence=0.7, source=self.name),
            Signal(PANEL_SIZE, numeric_value=float(panel), confidence=0.7, source=self.name),
        ]
