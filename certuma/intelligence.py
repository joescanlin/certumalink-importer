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
from certuma_core.intelligence import SignalView, fit_score, fit_tier, recommend_action

__all__ = ["OPEN_STATES", "recommended_actions"]

# leads still in play (everything but the terminal states)
OPEN_STATES = (
    "not_contacted", "queued_today", "enriching", "sendable", "email_sent",
    "awaiting_reply", "replied", "interested", "needs_review",
)


def _signals_for(session: Session, npi: str, when: datetime) -> Dict[str, SignalView]:
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
        .where(Lead.activation_status.in_(OPEN_STATES))
    ).all()

    out: List[dict] = []
    for lead, p in rows:
        signals = _signals_for(session, lead.npi, when)
        score = fit_score(signals)
        has_contact = session.execute(
            select(Contact.id).where(Contact.npi == lead.npi, Contact.email_status == "valid").limit(1)
        ).first() is not None
        due = lead.next_action_at is not None and lead.next_action_at <= when
        action, reason = recommend_action(lead.activation_status, has_contact=has_contact, due_now=due)
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
        })
    out.sort(key=lambda r: r["fit_score"], reverse=True)
    return out[:limit]
