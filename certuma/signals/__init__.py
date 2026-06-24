"""Clinician knowledge graph / signals (Phase 3 task P3.3).

The per-clinician signal store the proposal sells as the "knowledge graph + Clever Columns": each
clinician enriched with license, specialty board, region, group size, a public message-volume proxy,
and EHR / panel-size (behind the vendor seam). Deterministic stubs wire the public signals now; real
vendors slot in behind SignalProvider later. The stored signals feed trigger-signal scoring +
Recommended Actions (P3.4).
"""
from .provider import (EHR, GROUP_SIZE, MESSAGE_BURDEN, PANEL_SIZE, PUBLIC_ACTIVITY, SPECIALTY_BOARD,
                       STATE_LICENSE, ClinicianFacts, Signal, SignalProvider)
from .stub import PublicSignalProvider, VendorSignalProvider
from .node import (SignalSummary, collect_signals, default_providers, facts_for, run_signal_collection)

__all__ = [
    "Signal", "ClinicianFacts", "SignalProvider", "PublicSignalProvider", "VendorSignalProvider",
    "SignalSummary", "collect_signals", "run_signal_collection", "facts_for", "default_providers",
    "SPECIALTY_BOARD", "STATE_LICENSE", "GROUP_SIZE", "MESSAGE_BURDEN", "PUBLIC_ACTIVITY",
    "EHR", "PANEL_SIZE",
]
