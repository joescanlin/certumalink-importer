"""Circuit-breaker reader + writer (Phase 1 tasks P1.3 reader / P1.9 writer).

The Gate calls tripped_breaker() (read-only) to decide a HOLD. The ingest side (the monitor,
P1.9) calls record_outcome() to feed each delivery/bounce/complaint into a running rate and trip
the breaker when the rate crosses the threshold over a minimum sample. Read and write live in the
same module but on opposite call paths: the Gate never imports record_outcome, preserving its
no-write contract.

The trip thresholds below are PROVISIONAL defaults (the production window math is an open
stakeholder decision); they are deliberately conservative so a real deliverability problem pauses
sending rather than letting it run. Once tripped a breaker stays tripped until cleared by hand
(reset_breaker) - there is no auto-recovery in Phase 1.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from certuma.db.models import CircuitBreakerState
from certuma.observability import METRICS, emit, get_logger

__all__ = [
    "tripped_breaker",
    "record_outcome",
    "reset_breaker",
    "GLOBAL_SCOPE",
    "campaign_scope",
    "MIN_SAMPLE",
    "TRIP_RATE",
]

_LOG = get_logger("certuma.breakers")

GLOBAL_SCOPE = "global"

# Provisional trip policy. Below MIN_SAMPLE samples a breaker never trips (too little signal);
# at or above it, a running bad-rate >= TRIP_RATE for that breaker trips it.
MIN_SAMPLE = 20
TRIP_RATE = {"bounce": 0.05, "complaint": 0.001}  # 5% hard-bounce / 0.1% complaint


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


def _get_or_create(session: Session, scope: str, breaker: str) -> CircuitBreakerState:
    """Row-locked fetch of the (scope, breaker) state, inserting a zeroed row if absent.

    The insert is guarded by a savepoint so a concurrent inserter (unique scope+breaker) does not
    poison the caller's transaction; on collision we re-select the row the other writer created.
    """
    row = session.execute(
        select(CircuitBreakerState)
        .where(CircuitBreakerState.scope == scope, CircuitBreakerState.breaker == breaker)
        .with_for_update()
    ).scalar()
    if row is not None:
        return row
    try:
        with session.begin_nested():
            row = CircuitBreakerState(scope=scope, breaker=breaker)
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        return session.execute(
            select(CircuitBreakerState)
            .where(CircuitBreakerState.scope == scope, CircuitBreakerState.breaker == breaker)
            .with_for_update()
        ).scalar_one()


def record_outcome(
    session: Session,
    *,
    breaker: str,
    campaign: Optional[str],
    is_bad: bool,
) -> None:
    """Feed one outcome into the global (and campaign) breaker and trip it if the rate crosses.

    `breaker` is 'bounce' or 'complaint'; `is_bad` marks this sample as a bounce/complaint vs a
    clean delivery. Updates the running rate incrementally (rate = bad / samples) so no time-window
    scan is needed, and trips once samples >= MIN_SAMPLE and rate >= the breaker threshold. Idempotent
    re-trips are a no-op (tripped_at is set only on the first trip). The caller owns the transaction.
    """
    threshold = TRIP_RATE.get(breaker)
    if threshold is None:
        raise ValueError(f"unknown breaker {breaker!r}")
    scopes = [GLOBAL_SCOPE]
    cs = campaign_scope(campaign)
    if cs:
        scopes.append(cs)
    for scope in scopes:
        row = _get_or_create(session, scope, breaker)
        n = row.sample_count + 1
        bad = float(row.rate) * row.sample_count + (1 if is_bad else 0)
        row.sample_count = n
        row.rate = bad / n
        row.updated_at = func.now()
        if not row.is_tripped and n >= MIN_SAMPLE and row.rate >= threshold:
            row.is_tripped = True
            row.tripped_at = func.now()
            METRICS.incr("breaker_tripped", breaker=breaker, scope=scope)
            emit(_LOG, "breaker_tripped", breaker=breaker, scope=scope,
                 rate=round(float(row.rate), 4), samples=n)
    session.flush()


def reset_breaker(session: Session, *, breaker: str, scope: str = GLOBAL_SCOPE) -> None:
    """Manually clear a tripped breaker (operator action; no auto-recovery in Phase 1)."""
    row = session.execute(
        select(CircuitBreakerState)
        .where(CircuitBreakerState.scope == scope, CircuitBreakerState.breaker == breaker)
        .with_for_update()
    ).scalar()
    if row is None:
        return
    row.is_tripped = False
    row.tripped_at = None
    row.updated_at = func.now()
    session.flush()
    METRICS.incr("breaker_reset", breaker=breaker, scope=scope)
