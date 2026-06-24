"""Mailbox warmup ramp (Phase 3 task P3.10) - pure, no DB.

A brand-new cold-sending mailbox must not blast its full daily cap on day one or it torches its
domain reputation. warmup_cap ramps the allowed sends/day from a small start up to the mailbox's
target cap over WARMUP_DAYS, so the Gate's warmup check enforces a gradual ramp. Numbers are
provisional defaults to be tuned with the deliverability/ESP decision.
"""
from __future__ import annotations

from typing import Optional

__all__ = ["WARMUP_DAYS", "WARMUP_START_CAP", "warmup_cap"]

WARMUP_DAYS = 14        # days to ramp from the start cap up to the mailbox's target cap
WARMUP_START_CAP = 10   # sends/day allowed on day 0


def warmup_cap(target: int, age_days: Optional[float]) -> int:
    """The allowed sends/day for a mailbox `age_days` old whose fully-warmed target is `target`."""
    if age_days is None or age_days >= WARMUP_DAYS:
        return target
    if target <= WARMUP_START_CAP:
        return target
    frac = max(0.0, age_days) / WARMUP_DAYS
    return int(round(WARMUP_START_CAP + frac * (target - WARMUP_START_CAP)))
