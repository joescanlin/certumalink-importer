"""Support-agent node (Phase 4 / support).

handle_ticket files an inbound support message, classifies it, answers or escalates it, AND emits a
sales signal into the shared clinician_signal knowledge graph - so support compounds into sales
intelligence (an expansion question becomes an upsell lead; a complaint becomes a churn signal; a
rave becomes a referral lead). run_support is the batch entry (an autonomous support pass).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from certuma.db.models import ClinicianSignal, SupportTicket
from certuma.observability import METRICS, emit, get_logger

from .provider import EFFECTS, SUPPORT_INTENTS
from .stub import StubSupportClassifier

__all__ = ["SupportOutcome", "SupportSummary", "handle_ticket", "run_support", "emit_sales_signal",
           "reclassify", "override_intent", "set_status", "bulk_set_status"]

_LOG = get_logger("certuma.support")


@dataclass(frozen=True)
class SupportOutcome:
    intent: str
    status: str
    sales_signal: Optional[str]
    escalated: bool


@dataclass
class SupportSummary:
    classified: int = 0
    escalated: int = 0
    signals_emitted: int = 0
    npis: List[str] = field(default_factory=list)


def emit_sales_signal(session: Session, npi: str, signal_type: str, *, value: str,
                      when: datetime) -> None:
    """Upsert a support-derived sales signal into the shared knowledge graph (source='support')."""
    row = session.execute(
        select(ClinicianSignal).where(
            ClinicianSignal.npi == npi, ClinicianSignal.signal_type == signal_type,
            ClinicianSignal.source == "support")
    ).scalar()
    if row is None:
        session.add(ClinicianSignal(npi=npi, signal_type=signal_type, value=value, source="support",
                                    confidence=0.8, observed_at=when))
    else:
        row.value = value
        row.observed_at = when
    METRICS.incr("support_signal_emitted", signal=signal_type)


def _retract_sales_signal(session: Session, npi: str, signal_type: str) -> None:
    """Remove a support-derived signal once no ticket for this npi still emits it - so re-classifying
    or overriding a ticket away from a signal-bearing intent does not leave a phantom signal that
    sales scoring keeps reading. Guarded: a signal another ticket still asserts is left in place."""
    still = session.execute(
        select(SupportTicket.id).where(
            SupportTicket.npi == npi, SupportTicket.emitted_signal == signal_type).limit(1)
    ).first()
    if still is None:
        session.execute(delete(ClinicianSignal).where(
            ClinicianSignal.npi == npi, ClinicianSignal.signal_type == signal_type,
            ClinicianSignal.source == "support"))
        METRICS.incr("support_signal_retracted", signal=signal_type)


def _apply_effects(session: Session, ticket: SupportTicket, intent: str, *, when: datetime) -> SupportOutcome:
    """Apply an intent's canonical effects to a ticket (status, auto-answer, sales signal). Shared by
    the autonomous classifier and the operator's re-classify / override-intent actions."""
    old_signal = ticket.emitted_signal
    status, signal, answer = EFFECTS[intent]
    ticket.intent = intent
    ticket.status = status
    ticket.answer = answer
    ticket.emitted_signal = signal
    # preserve an existing resolution time; only stamp one when newly resolved, clear it when re-opened
    if status in ("answered", "resolved"):
        if ticket.resolved_at is None:
            ticket.resolved_at = when
    else:
        ticket.resolved_at = None
    session.flush()  # persist the new emitted_signal before reconciling the prior one
    if old_signal and old_signal != signal and ticket.npi:
        _retract_sales_signal(session, ticket.npi, old_signal)
    if signal and ticket.npi:
        emit_sales_signal(session, ticket.npi, signal, value=intent, when=when)
    session.flush()
    METRICS.incr("support_classified", intent=intent)
    emit(_LOG, "support_handled", ticket_id=ticket.id, npi=ticket.npi, intent=intent,
         status=status, sales_signal=signal)
    return SupportOutcome(intent=intent, status=status, sales_signal=signal, escalated=(status == "escalated"))


def _classify_and_handle(session: Session, ticket: SupportTicket, *, provider, when: datetime) -> SupportOutcome:
    result = provider.classify(text=ticket.body or "", context=ticket.subject or "")
    intent = result.intent if result.intent in SUPPORT_INTENTS else "other"
    return _apply_effects(session, ticket, intent, when=when)


def _get_ticket(session: Session, ticket_id: int) -> SupportTicket:
    ticket = session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise KeyError(ticket_id)
    return ticket


def reclassify(session: Session, ticket_id: int, *, provider=None,
               when: Optional[datetime] = None) -> Tuple[SupportTicket, SupportOutcome]:
    """Re-run the classifier on an existing ticket (e.g. after editing it). Caller commits."""
    provider = provider or StubSupportClassifier()
    when = when or datetime.now(timezone.utc)
    ticket = _get_ticket(session, ticket_id)
    outcome = _classify_and_handle(session, ticket, provider=provider, when=when)
    return ticket, outcome


def override_intent(session: Session, ticket_id: int, intent: str, *,
                    when: Optional[datetime] = None) -> Tuple[SupportTicket, SupportOutcome]:
    """An operator forces a specific intent; its canonical effects (status, signal) are re-applied."""
    if intent not in SUPPORT_INTENTS:
        raise ValueError(f"unknown intent {intent!r}")
    when = when or datetime.now(timezone.utc)
    ticket = _get_ticket(session, ticket_id)
    outcome = _apply_effects(session, ticket, intent, when=when)
    return ticket, outcome


def set_status(session: Session, ticket_id: int, status: str, *,
               when: Optional[datetime] = None) -> SupportTicket:
    """Mark a ticket resolved / escalated / answered / open without re-classifying it."""
    if status not in ("open", "answered", "escalated", "resolved"):
        raise ValueError(f"unknown status {status!r}")
    when = when or datetime.now(timezone.utc)
    ticket = _get_ticket(session, ticket_id)
    ticket.status = status
    ticket.resolved_at = when if status in ("answered", "resolved") else None
    session.flush()
    METRICS.incr("support_status_set", status=status)
    emit(_LOG, "support_status_set", ticket_id=ticket_id, status=status)
    return ticket


def bulk_set_status(session: Session, ticket_ids, status: str, *,
                    when: Optional[datetime] = None) -> int:
    """Apply a status to several tickets at once (the list-view bulk action). Returns the count."""
    count = 0
    for tid in ticket_ids:
        try:
            set_status(session, int(tid), status, when=when)
            count += 1
        except (KeyError, ValueError):
            continue
    return count


def handle_ticket(session: Session, *, npi: Optional[str], body: str, subject: str = "",
                  channel: str = "portal", provider=None, when: Optional[datetime] = None,
                  ) -> Tuple[SupportTicket, SupportOutcome]:
    """File one inbound support message and resolve it. Caller commits."""
    provider = provider or StubSupportClassifier()
    when = when or datetime.now(timezone.utc)
    ticket = SupportTicket(npi=npi, channel=channel, subject=subject, body=body, status="open")
    session.add(ticket)
    session.flush()
    outcome = _classify_and_handle(session, ticket, provider=provider, when=when)
    return ticket, outcome


def run_support(session: Session, *, provider=None, when: Optional[datetime] = None,
                limit: int = 200) -> SupportSummary:
    """Process every unclassified (open) support ticket autonomously. Caller commits."""
    provider = provider or StubSupportClassifier()
    when = when or datetime.now(timezone.utc)
    tickets = session.execute(
        select(SupportTicket).where(SupportTicket.intent.is_(None)).order_by(SupportTicket.id).limit(limit)
    ).scalars().all()

    summary = SupportSummary()
    for ticket in tickets:
        # date the emitted signal to when the ticket actually arrived, not the batch run time, so a
        # backlog of old tickets does not all surface as fresh, full-weight signals.
        outcome = _classify_and_handle(session, ticket, provider=provider,
                                       when=(ticket.created_at or when))
        summary.classified += 1
        if outcome.escalated:
            summary.escalated += 1
        if outcome.sales_signal and ticket.npi:  # a signal is only emitted when there is an npi to attach it to
            summary.signals_emitted += 1
            summary.npis.append(ticket.npi)
    session.flush()
    METRICS.incr("support_run")
    emit(_LOG, "support_run", classified=summary.classified, escalated=summary.escalated,
         signals=summary.signals_emitted)
    return summary
