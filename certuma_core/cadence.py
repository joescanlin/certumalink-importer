"""Cadence scheduling policy (Phase 2 task P2.4) - pure, no DB.

A first touch (cadence_step 0) is followed by up to MAX_STEP more touches, spaced by INTERVAL_DAYS,
unless the lead replies, is suppressed, or activates first. The numbers are PROVISIONAL defaults
(to be confirmed with stakeholder + made campaign-configurable); they live here, pure and shared,
so the monitor (which schedules the first follow-up on delivery) and the cadence engine (which
sends the follow-ups) agree without importing each other.
"""
from __future__ import annotations

from datetime import datetime, timedelta

__all__ = ["MAX_STEP", "INTERVAL_DAYS", "FINAL_GRACE_DAYS", "next_action_after", "is_final_step"]

MAX_STEP = 2                 # first touch (step 0) + up to 2 follow-ups (steps 1, 2)
INTERVAL_DAYS = (3, 7)       # days to wait before step 1 (from step 0) and step 2 (from step 1)
FINAL_GRACE_DAYS = 7         # after the last touch, wait this long for a reply before exhausting


def next_action_after(step: int, when: datetime) -> datetime:
    """When the NEXT touch is due, given we just acted at `step`.

    For a non-final step, wait INTERVAL_DAYS[step] before the next follow-up. After the final step,
    return a grace window so the lead is revisited once more and then exhausted (no reply).
    """
    if step < 0:
        step = 0
    if step < MAX_STEP:
        return when + timedelta(days=INTERVAL_DAYS[step])
    return when + timedelta(days=FINAL_GRACE_DAYS)


def is_final_step(step: int) -> bool:
    """True if `step` is the last touch we will send (no further follow-up after it)."""
    return step >= MAX_STEP
