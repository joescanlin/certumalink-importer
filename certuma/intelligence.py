"""Recommended Actions / fit ranking (Phase 3 task P3.4) - the DB wrapper over the pure scoring.

recommended_actions loads each open lead's knowledge-graph signals, computes its fit score and
next-best-action, and ranks the queue top-fit first - the proposal's "Rox scores and ranks by fit +
trigger signals; sequences top tiers first" + "Recommended Actions." Read-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.db.models import ClinicianSignal, Contact, Lead, Prospect
from certuma_core.intelligence import (SignalView, fit_score, fit_tier, recommend_action,
                                       support_action)

__all__ = ["OPEN_STATES", "recommended_actions"]

# leads still in play (everything but the terminal states)
OPEN_STATES = (
    "not_contacted", "queued_today", "enriching", "sendable", "email_sent",
    "awaiting_reply", "replied", "interested", "needs_review",
)
# activated customers are "done" for outbound, but a support signal (upsell / churn / referral)
# turns them back into a sales action - so they re-enter the recommended queue when, and only when,
# support has surfaced an opportunity on them.
_SUPPORT_REENTRY = ("physician_activated",)


def _signals_for(session: Session, npi: str, when: datetime) -> Dict[str, SignalView]:
    # keyed by signal_type alone; this assumes signal_type is globally unique across sources (the
    # support types are disjoint from the provider types today). If that ever stops holding, key by
    # (signal_type, source) instead, since the DB uniqueness is (npi, signal_type, source).
    out: Dict[str, SignalView] = {}
    for s in session.execute(
        select(ClinicianSignal).where(ClinicianSignal.npi == npi)
    ).scalars():
        age = max(0.0, (when - s.observed_at).total_seconds() / 86400.0) if s.observed_at else 0.0
        out[s.signal_type] = SignalView(
            value=s.value or "",
            numeric=(float(s.numeric_value) if s.numeric_value is not None else None),
            confidence=float(s.confidence), age_days=age)
    return out


def recommended_actions(session: Session, *, when: Optional[datetime] = None, limit: int = 50) -> List[dict]:
    """Open leads ranked by fit score, each with its next-best-action. Read-only."""
    when = when or datetime.now(timezone.utc)
    rows = session.execute(
        select(Lead, Prospect).join(Prospect, Lead.npi == Prospect.npi)
        .where(Lead.activation_status.in_(OPEN_STATES + _SUPPORT_REENTRY))
    ).all()

    out: List[dict] = []
    for lead, p in rows:
        signals = _signals_for(session, lead.npi, when)
        sup = support_action(signals)
        # an activated customer is only a sales action when support surfaced an opportunity on them
        if lead.activation_status in _SUPPORT_REENTRY and sup is None:
            continue
        score = fit_score(signals)
        has_contact = session.execute(
            select(Contact.id).where(Contact.npi == lead.npi, Contact.email_status == "valid").limit(1)
        ).first() is not None
        due = lead.next_action_at is not None and lead.next_action_at <= when
        if sup is not None:
            action, reason, urgency = sup  # a support signal takes precedence as the next best action
        else:
            action, reason = recommend_action(lead.activation_status, has_contact=has_contact, due_now=due)
            urgency = 0
        out.append({
            "npi": lead.npi,
            "name": p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi,
            "specialty": p.primary_specialty or "",
            "state": p.practice_state or "",
            "status": lead.activation_status,
            "fit_score": score,
            "fit_tier": fit_tier(score),
            "action": action,
            "reason": reason,
            "urgency": urgency,
        })
    # support-driven actions (esp. churn/retention, urgency 2) sort to the top regardless of fit, so a
    # churning customer - whose fit the churn signal deliberately drives down - is never truncated off
    # the bottom of the queue. Within an urgency band, rank by fit.
    out.sort(key=lambda r: (r["urgency"], r["fit_score"]), reverse=True)
    return out[:limit]
