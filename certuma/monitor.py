"""Event ingestion + deterministic lifecycle monitor (Phase 1 task P1.9).

This is the inbound half of the loop: provider/webhook/poller signals arrive here as normalized
events, get deduplicated, and drive deterministic ledger effects. NOTHING here is LLM-gated - a
delivery, a bounce, an opt-out, and an activation are all mechanical state changes governed by
ALLOWED_TRANSITIONS and the single ledger_writer. Suppression (the CAN-SPAM / deliverability
safety net) is recorded BEFORE the lead transition so even a late/illegal transition (e.g. the
lead already moved to a terminal state) still leaves the durable do-not-contact record.

Effect map (event_type -> effect):
  delivered          mark message.delivered; email_sent -> awaiting_reply; feed bounce breaker (clean)
  bounced            mark message.bounced; suppress(hard_bounce, the dead address); needs_reenrich;
                     -> exhausted; feed bounce breaker (bad)
  complained         mark message.complained; suppress(complaint); -> do_not_contact; feed complaint breaker (bad)
  opt_out            suppress(opt_out); -> do_not_contact
  unsubscribe_click  suppress(opt_out); -> do_not_contact
  activated          activate_lead(actor='activation_webhook')  [the future platform webhook path]
  opened/sent/replied  recorded only (no transition; reply classification is Phase 2)

Dedup is enforced by the uq_event_dedup unique index: record_event inserts inside a savepoint and
returns None on collision, so a webhook redelivery or an overlapping poll is a no-op. Idempotency
of the *effects* additionally rides on ALLOWED_TRANSITIONS (a second delivered cannot re-fire
awaiting_reply -> awaiting_reply) and on the suppression unique indexes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from certuma import breakers, gate
from certuma.db.models import Event, Lead, Message, Suppression
from certuma.ledger_writer import IllegalActor, transition
from certuma.observability import METRICS, emit, get_logger
from certuma_core.status import IllegalTransition

__all__ = ["IngestResult", "record_event", "ingest_event", "activate_lead",
           "suppress", "try_transition"]

_LOG = get_logger("certuma.monitor")

# event_type -> the suppression reason it records (if any) and whether it is a "bad" breaker sample.
_SUPPRESS_REASON = {
    "bounced": "hard_bounce",
    "complained": "complaint",
    "opt_out": "opt_out",
    "unsubscribe_click": "opt_out",
}


@dataclass(frozen=True)
class IngestResult:
    duplicate: bool
    event_id: Optional[int] = None
    transitioned_to: Optional[str] = None  # the lead's new status, if a transition fired
    suppressed: bool = False
    activated: bool = False


def _now(when: Optional[datetime]) -> datetime:
    return when or datetime.now(timezone.utc)


def record_event(
    session: Session,
    *,
    event_type: str,
    dedup_key: str,
    occurred_at: datetime,
    lead_id: Optional[int] = None,
    message_id: Optional[int] = None,
    npi: Optional[str] = None,
    payload: Optional[Mapping[str, object]] = None,
) -> Optional[int]:
    """Insert one event idempotently. Returns the new event id, or None if already seen (dedup).

    The insert rides a savepoint so a duplicate dedup_key (uq_event_dedup) does not poison the
    caller's transaction. The caller owns commit/rollback.
    """
    try:
        with session.begin_nested():
            ev = Event(
                dedup_key=dedup_key, event_type=event_type, occurred_at=occurred_at,
                lead_id=lead_id, message_id=message_id, npi=npi, payload=dict(payload or {}),
            )
            session.add(ev)
            session.flush()
        METRICS.incr("event_ingested", type=event_type)
        return ev.id
    except IntegrityError:
        METRICS.incr("event_duplicate", type=event_type)
        return None


def _resolve_lead(
    session: Session, *, lead_id: Optional[int], message_id: Optional[int], npi: Optional[str]
) -> Optional[Lead]:
    if lead_id is not None:
        return session.get(Lead, lead_id)
    if message_id is not None:
        msg = session.get(Message, message_id)
        if msg is not None:
            return session.get(Lead, msg.lead_id)
        return None
    if npi:
        # The most recent non-terminal lead for this prospect (one-per-campaign; demo has one).
        return session.execute(
            select(Lead).where(
                Lead.npi == npi,
                Lead.activation_status.notin_(("physician_activated", "do_not_contact", "exhausted")),
            ).order_by(Lead.id.desc()).limit(1)
        ).scalar()
    return None


def _suppress(session: Session, *, reason: str, npi: Optional[str], email: Optional[str], source: str) -> bool:
    """Idempotently record a suppression for whatever keys are present. Returns True if it added one.

    Pre-checks gate.is_suppressed (which ORs npi/email) to skip the common already-suppressed case,
    and rides a savepoint to absorb a racing insert against uq_suppress_npi / uq_suppress_email.
    """
    if not npi and not email:
        return False
    if gate.is_suppressed(session, npi=npi, email=email):
        return False
    try:
        with session.begin_nested():
            session.add(Suppression(npi=npi, email=email, reason=reason, source=source))
            session.flush()
        METRICS.incr("suppression_added", reason=reason)
        return True
    except IntegrityError:
        return False


def _try_transition(session: Session, lead: Lead, new_status: str, *, actor: str, reason_code: str) -> bool:
    """Transition the lead, treating an illegal/terminal-source move as a no-op (late event).

    A delivered after an opt-out, a bounce on an already-exhausted lead, etc. must not raise: the
    durable suppression is already recorded; the lead simply does not move.
    """
    try:
        transition(session, lead.id, new_status, actor=actor,
                   reason_code=reason_code, expected_version=lead.version)
        return True
    except (IllegalTransition, IllegalActor):
        METRICS.incr("monitor_transition_skipped", to=new_status)
        emit(_LOG, "monitor_transition_skipped", lead_id=lead.id,
             frm=lead.activation_status, to=new_status, reason_code=reason_code)
        return False


# Public aliases so the reply classifier (P2.2) records suppressions and transitions through the
# exact same compliance-critical paths as the deterministic monitor (one implementation).
suppress = _suppress
try_transition = _try_transition


def activate_lead(
    session: Session, lead: Lead, *, actor: str, when: Optional[datetime] = None, reason_code: str = "claim_click"
) -> bool:
    """Drive a sent lead to physician_activated: -> interested (if needed) -> physician_activated.

    Returns True if it reached physician_activated this call, False if already activated or the
    lead cannot legally reach interested (e.g. suppressed/terminal). `actor` must be in
    ACTIVATION_ONLY_ACTORS ('poller' | 'activation_webhook'); the ledger_writer enforces that.
    """
    when = _now(when)
    if lead.activation_status == "physician_activated":
        return False
    if lead.activation_status != "interested":
        if not _try_transition(session, lead, "interested", actor=actor, reason_code=reason_code):
            return False
    if not _try_transition(session, lead, "physician_activated", actor=actor, reason_code=reason_code):
        return False
    lead.activation_detected_at = when
    session.flush()
    METRICS.incr("lead_activated", actor=actor)
    emit(_LOG, "lead_activated", lead_id=lead.id, npi=lead.npi, actor=actor)
    return True


def ingest_event(
    session: Session,
    *,
    event_type: str,
    dedup_key: str,
    occurred_at: datetime,
    lead_id: Optional[int] = None,
    message_id: Optional[int] = None,
    npi: Optional[str] = None,
    email: Optional[str] = None,
    payload: Optional[Mapping[str, object]] = None,
    when: Optional[datetime] = None,
) -> IngestResult:
    """Ingest one normalized inbound event and apply its deterministic effect. Caller owns the txn."""
    when = _now(when)
    event_id = record_event(
        session, event_type=event_type, dedup_key=dedup_key, occurred_at=occurred_at,
        lead_id=lead_id, message_id=message_id, npi=npi, payload=payload,
    )
    if event_id is None:
        return IngestResult(duplicate=True)

    lead = _resolve_lead(session, lead_id=lead_id, message_id=message_id, npi=npi)
    msg = session.get(Message, message_id) if message_id is not None else None
    result = IngestResult(duplicate=False, event_id=event_id)

    if event_type == "delivered":
        if msg is not None:
            msg.delivered = True
        breakers.record_outcome(session, breaker="bounce",
                                campaign=(lead.campaign if lead else None), is_bad=False)
        if lead is not None and _try_transition(session, lead, "awaiting_reply",
                                                actor="monitor", reason_code="delivered"):
            result = IngestResult(duplicate=False, event_id=event_id, transitioned_to="awaiting_reply")

    elif event_type == "bounced":
        if msg is not None:
            msg.bounced = True
        # Suppress the dead ADDRESS (not the npi) so a re-enriched contact can be retried later.
        supp = _suppress(session, reason="hard_bounce", npi=(None if email else npi), email=email,
                         source="esp_bounce")
        if lead is not None:
            lead.needs_reenrich = True
            moved = _try_transition(session, lead, "exhausted", actor="monitor", reason_code="hard_bounce")
        else:
            moved = False
        breakers.record_outcome(session, breaker="bounce",
                                campaign=(lead.campaign if lead else None), is_bad=True)
        result = IngestResult(duplicate=False, event_id=event_id,
                              transitioned_to="exhausted" if moved else None, suppressed=supp)

    elif event_type == "complained":
        if msg is not None:
            msg.complained = True
        supp = _suppress(session, reason="complaint", npi=npi, email=email, source="esp_complaint")
        moved = lead is not None and _try_transition(session, lead, "do_not_contact",
                                                     actor="monitor", reason_code="complaint")
        breakers.record_outcome(session, breaker="complaint",
                                campaign=(lead.campaign if lead else None), is_bad=True)
        result = IngestResult(duplicate=False, event_id=event_id,
                              transitioned_to="do_not_contact" if moved else None, suppressed=supp)

    elif event_type in ("opt_out", "unsubscribe_click"):
        supp = _suppress(session, reason="opt_out", npi=npi, email=email, source=event_type)
        moved = lead is not None and _try_transition(session, lead, "do_not_contact",
                                                     actor="monitor", reason_code="opt_out")
        result = IngestResult(duplicate=False, event_id=event_id,
                              transitioned_to="do_not_contact" if moved else None, suppressed=supp)

    elif event_type == "activated":
        activated = lead is not None and activate_lead(
            session, lead, actor="activation_webhook", when=when, reason_code="activation_webhook")
        result = IngestResult(duplicate=False, event_id=event_id,
                              transitioned_to="physician_activated" if activated else None,
                              activated=activated)

    # opened / sent / replied: recorded only in Phase 1 (no transition).
    session.flush()
    return result
