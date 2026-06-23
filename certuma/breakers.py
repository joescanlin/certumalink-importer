"""Circuit-breaker READER (Phase 1 task P1.3).

Read-only. The Gate calls tripped_breaker() to decide a HOLD; the ingest side (P1.9) is what
actually trips/clears circuit_breaker_state. Keeping the read and the write on opposite sides
preserves the Gate's no-write contract.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.db.models import CircuitBreakerState

__all__ = ["tripped_breaker", "GLOBAL_SCOPE", "campaign_scope"]

GLOBAL_SCOPE = "global"


def campaign_scope(campaign: Optional[str]) -> Optional[str]:
    return f"campaign:{campaign}" if campaign else None


def tripped_breaker(session: Session, campaign: Optional[str] = None) -> Optional[str]:
    """Return 'complaint'/'bounce' if a breaker is tripped for the global or campaign scope, else None."""
    scopes = [GLOBAL_SCOPE]
    cs = campaign_scope(campaign)
    if cs:
        scopes.append(cs)
    row = session.execute(
        select(CircuitBreakerState.breaker)
        .where(CircuitBreakerState.scope.in_(scopes), CircuitBreakerState.is_tripped.is_(True))
        .limit(1)
    ).scalar()
    return row
