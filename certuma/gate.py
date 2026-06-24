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

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from certuma.breakers import tripped_breaker
from certuma.compliance import can_spam_incomplete
from certuma.config import Settings
from certuma.db.models import Campaign, KillSwitch, Message, Prospect, Suppression
from certuma.observability import METRICS, emit, get_logger
from certuma_core.quiet_hours import is_quiet_hours

__all__ = ["ALLOW", "HOLD", "BLOCK", "GateDecision", "evaluate", "is_suppressed", "operational_hold"]

ALLOW = "ALLOW"
HOLD = "HOLD"
BLOCK = "BLOCK"

_LOG = get_logger("certuma.gate")


def _decided(decision: "GateDecision") -> "GateDecision":
    METRICS.incr("gate_decision", decision=decision.decision, reason=decision.reason_code or "")
    emit(_LOG, "gate_decision", level=logging.DEBUG,
         decision=decision.decision, reason_code=decision.reason_code)
    return decision


@dataclass(frozen=True)
class GateDecision:
    decision: str
    reason_code: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


def is_suppressed(session: Session, npi: Optional[str] = None, email: Optional[str] = None) -> bool:
    """Public suppression check (used by the enricher before spending on enrichment)."""
    return _is_suppressed(session, npi, email)


def operational_hold(session: Session, *, npi: Optional[str], campaign: Optional[str] = None) -> Optional[str]:
    """Channel-AGNOSTIC operational controls: suppression, the global kill switch, and per-campaign
    pause. Returns a reason ('suppression'|'kill_switch'|'campaign_paused') or None. Non-email
    channels (e.g. LinkedIn) honor these even though they skip the EMAIL-specific Gate checks
    (CAN-SPAM completeness, quiet-hours-by-mailbox-TZ, warmup caps, deliverability breakers)."""
    if _is_suppressed(session, npi, None):
        return "suppression"
    if session.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar():
        return "kill_switch"
    if campaign is not None:
        paused = session.execute(
            select(Campaign.is_paused).where(Campaign.name == campaign)
        ).scalar()
        if paused:
            return "campaign_paused"
    return None


def _over_warmup_cap(session: Session, mailbox, when_utc: datetime) -> bool:
    start = datetime(when_utc.year, when_utc.month, when_utc.day, tzinfo=timezone.utc)
    sent = session.execute(
        select(func.count()).select_from(Message).where(
            Message.mailbox_id == mailbox.id, Message.sent_at.isnot(None), Message.sent_at >= start
        )
    ).scalar()
    return sent >= mailbox.daily_cap


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
    when: Optional[datetime] = None,
    mailbox=None,
    settings: Optional[Settings] = None,
) -> GateDecision:
    """Decide whether an outbound action may proceed. Read-only; performs no transition.

    Phase 1 adds (all opt-in via keyword, so /gate/preview and Phase 0 callers are unchanged):
    circuit-breaker HOLD (always read), CAN-SPAM HOLD (when `settings` given), quiet-hours HOLD
    (when `when` given), warmup-cap HOLD (when `mailbox` given). New checks are read-only.
    """
    # 1. suppression is the permanent BLOCK floor (checked first, by npi AND email)
    if _is_suppressed(session, npi, email):
        return _decided(GateDecision(BLOCK, "suppression"))

    # 2. global kill switch -> HOLD
    if session.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar():
        return _decided(GateDecision(HOLD, "kill_switch"))

    # 3. per-campaign pause -> HOLD
    if campaign is not None:
        paused = session.execute(
            select(Campaign.is_paused).where(Campaign.name == campaign)
        ).scalar()
        if paused:
            return _decided(GateDecision(HOLD, "campaign_paused"))

    # 4. circuit breaker tripped (complaint/bounce) -> HOLD. Read-only; the ingest side trips it.
    breaker = tripped_breaker(session, campaign)
    if breaker:
        return _decided(GateDecision(HOLD, f"circuit_breaker_{breaker}"))

    # 5. CAN-SPAM completeness (only when settings are supplied; a re-queueable config HOLD,
    #    NOT a BLOCK). Skipped on /gate/preview so Phase 0 behavior is unchanged.
    if settings is not None:
        reason = can_spam_incomplete(session, campaign, settings)
        if reason:
            return _decided(GateDecision(HOLD, "can_spam_incomplete"))

    # 6. quiet hours (only when a send time is supplied) -> HOLD
    if when is not None:
        state = session.execute(select(Prospect.practice_state).where(Prospect.npi == npi)).scalar()
        if is_quiet_hours(state or "", when):
            return _decided(GateDecision(HOLD, "quiet_hours"))

    # 7. warmup cap (only when a mailbox is supplied) -> HOLD
    if mailbox is not None and _over_warmup_cap(session, mailbox, when or datetime.now(timezone.utc)):
        return _decided(GateDecision(HOLD, "warmup_cap_exceeded"))

    return _decided(GateDecision(ALLOW, None))
