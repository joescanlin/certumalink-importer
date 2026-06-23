"""The single ledger-writer (Phase 0 task B8).

This module is the ONLY code path permitted to write lead.activation_status. Every transition
is, in one atomic step: row-locked, optimistic-concurrency checked, legality-checked against
certuma_core.status.ALLOWED_TRANSITIONS, actor-guarded (only the poller/activation_webhook may
set physician_activated, protecting the sole conversion metric), optionally idempotency-keyed
(the outbound message row is inserted BEFORE any upstream ESP call so a crash/retry cannot
double-send), and audit-logged. The importer's seed-upsert path never imports this module and
therefore structurally cannot change status.

The caller owns the transaction boundary (commit/rollback). transition() flushes so that an
idempotency collision surfaces immediately as IntegrityError.
"""
from __future__ import annotations

from typing import Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from certuma_core.status import ACTIVATION_ONLY_ACTORS, IllegalTransition, assert_transition
from certuma.db.models import AuditLog, Lead, Message
from certuma.observability import METRICS, emit, get_logger

__all__ = ["ConcurrencyConflict", "IllegalActor", "IllegalTransition", "transition"]

_LOG = get_logger("certuma.ledger")


class ConcurrencyConflict(Exception):
    """Optimistic-concurrency failure: the lead changed since the caller read it."""

    def __init__(self, lead_id: int, expected: int, actual: int):
        super().__init__(f"lead {lead_id}: expected version {expected}, found {actual}")
        self.lead_id, self.expected, self.actual = lead_id, expected, actual


class IllegalActor(Exception):
    """An actor attempted a transition reserved for other actors (e.g. activation)."""


def transition(
    session: Session,
    lead_id: int,
    new_status: str,
    *,
    actor: str,
    reason_code: str,
    expected_version: int,
    idempotency: Optional[Mapping[str, object]] = None,
) -> Lead:
    """Atomically move a lead to new_status. Returns the updated (flushed) Lead.

    Raises ConcurrencyConflict, IllegalTransition, IllegalActor, or (on a duplicate
    idempotency key) sqlalchemy IntegrityError. Does not commit.
    """
    # 1. lock the row
    lead = session.execute(
        select(Lead).where(Lead.id == lead_id).with_for_update()
    ).scalar_one()

    # 2. optimistic concurrency
    if lead.version != expected_version:
        METRICS.incr("ledger_rejected", reason="concurrency")
        raise ConcurrencyConflict(lead_id, expected_version, lead.version)

    # 3. legality (raises IllegalTransition; terminal-safe)
    try:
        assert_transition(lead.activation_status, new_status)
    except IllegalTransition:
        METRICS.incr("ledger_rejected", reason="illegal_transition")
        raise

    # 3b. sole-success-metric guard: only the poller/webhook may activate
    if new_status == "physician_activated" and actor not in ACTIVATION_ONLY_ACTORS:
        METRICS.incr("ledger_rejected", reason="illegal_actor")
        raise IllegalActor(f"actor {actor!r} may not set physician_activated")

    # 4. idempotency: insert the outbound message key BEFORE the ESP call upstream.
    #    Duplicate (npi, campaign, cadence_step) outbound -> IntegrityError (partial UNIQUE).
    if idempotency is not None:
        session.add(Message(**dict(idempotency)))
        session.flush()

    # 5. write + bump version + audit, same transaction
    old_status = lead.activation_status
    lead.activation_status = new_status
    lead.version += 1
    lead.updated_at = func.now()
    session.add(
        AuditLog(
            entity="lead",
            entity_id=str(lead.id),
            npi=lead.npi,
            action="transition",
            old_value={"status": old_status},
            new_value={"status": new_status},
            actor=actor,
            reason_code=reason_code,
        )
    )
    session.flush()
    METRICS.incr("ledger_transition", new=new_status)
    emit(_LOG, "lead_transition", lead_id=lead.id, npi=lead.npi,
         old=old_status, new=new_status, actor=actor, reason_code=reason_code)
    return lead
