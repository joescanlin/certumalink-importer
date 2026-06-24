"""Clinician signal interfaces + value types (Phase 3 task P3.3). Pure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol

__all__ = [
    "Signal", "ClinicianFacts", "SignalProvider",
    "SPECIALTY_BOARD", "STATE_LICENSE", "GROUP_SIZE", "MESSAGE_BURDEN", "PUBLIC_ACTIVITY",
    "EHR", "PANEL_SIZE",
]

# signal types (the knowledge-graph "columns")
SPECIALTY_BOARD = "specialty_board"
STATE_LICENSE = "state_license"
GROUP_SIZE = "group_size"
MESSAGE_BURDEN = "message_burden"     # public message-volume proxy (the deck's signal)
PUBLIC_ACTIVITY = "public_activity"
EHR = "ehr"                           # gated/paid vendor signal
PANEL_SIZE = "panel_size"             # gated/paid vendor signal


@dataclass(frozen=True)
class Signal:
    signal_type: str
    value: str = ""
    numeric_value: Optional[float] = None
    confidence: float = 1.0
    source: str = ""


@dataclass(frozen=True)
class ClinicianFacts:
    npi: str
    first_name: str = ""
    last_name: str = ""
    specialty: str = ""
    state: str = ""
    city: str = ""
    group_size: int = 0


class SignalProvider(Protocol):
    name: str

    def signals(self, facts: ClinicianFacts) -> List[Signal]:
        ...
