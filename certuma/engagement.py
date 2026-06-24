"""Engagement plays / re-engage + churn-risk queue (Phase 3 task P3.6) - the DB wrapper.

engagement_queue scans open, not-yet-converted leads, classifies each from its engagement rollup,
and returns the ones that warrant an operator play (opened-no-reply, went-quiet, churn-risk), ordered
churn-risk first. Read-only; opens are weak, so this surfaces suggestions, it does not auto-send.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.db.models import Lead, Prospect
from certuma_core.engagement import (CHURN_RISK, FLAGGED, OPENED_NO_REPLY, WENT_QUIET, classify,
                                     play_for)

__all__ = ["ENGAGED_STATES", "engagement_queue"]

ENGAGED_STATES = ("email_sent", "awaiting_reply", "interested")
_ORDER = {CHURN_RISK: 0, WENT_QUIET: 1, OPENED_NO_REPLY: 2}


def engagement_queue(session: Session, *, now: Optional[datetime] = None, limit: int = 50) -> List[dict]:
    """Open leads that opened but have not converted, flagged with an engagement play. Read-only."""
    now = now or datetime.now(timezone.utc)
    rows = session.execute(
        select(Lead, Prospect).join(Prospect, Lead.npi == Prospect.npi)
        .where(Lead.activation_status.in_(ENGAGED_STATES), Lead.open_count > 0)
    ).all()

    out: List[dict] = []
    for lead, p in rows:
        days = ((now - lead.last_engaged_at).total_seconds() / 86400.0) if lead.last_engaged_at else None
        replied = lead.activation_status == "interested"  # a positive reply already happened
        state = classify(replied=replied, open_count=lead.open_count or 0,
                         days_since_engaged=days, cadence_step=lead.cadence_step or 0)
        if state not in FLAGGED:
            continue
        out.append({
            "npi": lead.npi,
            "name": p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi,
            "specialty": p.primary_specialty or "",
            "status": lead.activation_status,
            "open_count": lead.open_count or 0,
            "days_quiet": (round(days, 1) if days is not None else None),
            "state": state,
            "play": play_for(state),
        })
    out.sort(key=lambda r: (_ORDER.get(r["state"], 9), -r["open_count"]))
    return out[:limit]
