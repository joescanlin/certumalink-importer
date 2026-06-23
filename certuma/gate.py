"""Compliance & Deliverability Gate, Phase-0 stub (task B9).

Every future outbound message will pass through this single chokepoint. Phase 0 implements
only the controls that exist today: the suppression BLOCK floor and the kill-switch /
campaign-pause HOLDs. Later phases add CAN-SPAM completeness, quiet hours, warmup caps, and
the complaint/bounce circuit breakers.

Returns ALLOW / HOLD / BLOCK plus a reason_code. The Gate NEVER transitions a lead: a HOLD is
a no-op that leaves the lead where it is to be re-queued. BLOCK (suppression) is the permanent
floor and takes precedence over the switches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from certuma.db.models import Campaign, KillSwitch, Suppression

__all__ = ["ALLOW", "HOLD", "BLOCK", "GateDecision", "evaluate"]

ALLOW = "ALLOW"
HOLD = "HOLD"
BLOCK = "BLOCK"


@dataclass(frozen=True)
class GateDecision:
    decision: str
    reason_code: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


def _is_suppressed(session: Session, npi: Optional[str], email: Optional[str]) -> bool:
    # Build conditions only for present keys so a NULL key cannot match NULL rows.
    conds = []
    if npi:
        conds.append(Suppression.npi == npi)
    if email:
        conds.append(Suppression.email == email)
    if not conds:
        return False
    return session.execute(select(Suppression.id).where(or_(*conds)).limit(1)).first() is not None


def evaluate(
    session: Session,
    *,
    npi: Optional[str],
    email: Optional[str],
    campaign: Optional[str],
) -> GateDecision:
    """Decide whether an outbound action may proceed. Read-only; performs no transition."""
    # 1. suppression is the permanent BLOCK floor (checked first, by npi AND email)
    if _is_suppressed(session, npi, email):
        return GateDecision(BLOCK, "suppression")

    # 2. global kill switch -> HOLD
    if session.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar():
        return GateDecision(HOLD, "kill_switch")

    # 3. per-campaign pause -> HOLD
    if campaign is not None:
        paused = session.execute(
            select(Campaign.is_paused).where(Campaign.name == campaign)
        ).scalar()
        if paused:
            return GateDecision(HOLD, "campaign_paused")

    return GateDecision(ALLOW, None)
