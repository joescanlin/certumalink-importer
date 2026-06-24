"""Engagement-signal plays (Phase 3 task P3.6) - pure, no DB.

Turns the engagement rollup (opens, last engagement) into a play: a lead that opened but has not
replied is high-intent (reply-bump); one that engaged and then went silent past a window needs a
re-engagement angle; one that engaged but is near the end of its cadence with no movement is a churn
risk to flag. These are the proposal's "Usage Signal Plays - re-engage when a clinician goes quiet"
and "Churn Risk - disengagement flagged early." Opens are weak (latent issue 1), so a play is a
suggestion surfaced to the operator, never an automatic high-stakes send.
"""
from __future__ import annotations

from typing import Optional

from certuma_core.cadence import MAX_STEP

__all__ = ["QUIET_DAYS", "COLD", "OPENED_NO_REPLY", "WENT_QUIET", "CHURN_RISK", "REPLIED",
           "classify", "play_for", "FLAGGED"]

QUIET_DAYS = 10  # engaged, then silent this long -> went quiet (provisional)

COLD = "cold"
OPENED_NO_REPLY = "opened_no_reply"
WENT_QUIET = "went_quiet"
CHURN_RISK = "churn_risk"
REPLIED = "replied"

FLAGGED = (CHURN_RISK, WENT_QUIET, OPENED_NO_REPLY)  # states that warrant an operator play

_PLAY = {
    OPENED_NO_REPLY: "Reply-bump (opened, high intent)",
    WENT_QUIET: "Re-engage with a new angle",
    CHURN_RISK: "Flag for a personal touch",
}


def classify(*, replied: bool, open_count: int, days_since_engaged: Optional[float], cadence_step: int) -> str:
    """The engagement state for one open, not-yet-converted lead."""
    if replied:
        return REPLIED                          # already in a conversation; not a re-engage target
    if (open_count or 0) <= 0:
        return COLD                             # never opened; ordinary cadence handles it
    if days_since_engaged is None or days_since_engaged < QUIET_DAYS:
        return OPENED_NO_REPLY                  # opened recently, no reply -> high intent
    if cadence_step >= MAX_STEP:
        return CHURN_RISK                       # engaged once, now silent and out of touches
    return WENT_QUIET                           # engaged, then quiet, but touches remain


def play_for(state: str) -> Optional[str]:
    return _PLAY.get(state)
