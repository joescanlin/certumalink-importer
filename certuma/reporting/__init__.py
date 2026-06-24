"""Analytics reporting layer (Phase 3).

A separate read-optimized `reporting` schema (facts + dimensions) materialized from the operational
tables by the ELT - the evidence + Customer-Intelligence layer the Rox proposal sells. Never a second
writer of operational state; rebuildable from scratch; suppression-aware so the analytics and the
Series-A export inherit opt-out.
"""
from .elt import RebuildReport, SEND_COST, rebuild, run_rebuild

__all__ = ["RebuildReport", "SEND_COST", "rebuild", "run_rebuild"]
