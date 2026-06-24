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

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.db.models import ClinicianSignal, SupportTicket
from certuma.observability import METRICS, emit, get_logger

from .provider import EFFECTS, SUPPORT_INTENTS
from .stub import StubSupportClassifier

__all__ = ["SupportOutcome", "SupportSummary", "handle_ticket", "run_support", "emit_sales_signal"]

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


def _classify_and_handle(session: Session, ticket: SupportTicket, *, provider, when: datetime) -> SupportOutcome:
    result = provider.classify(text=ticket.body or "", context=ticket.subject or "")
    intent = result.intent if result.intent in SUPPORT_INTENTS else "other"
    status, signal, answer = EFFECTS[intent]
    ticket.intent = intent
    ticket.status = status
    ticket.answer = answer
    ticket.emitted_signal = signal
    if status in ("answered", "resolved"):
        ticket.resolved_at = when
    if signal and ticket.npi:
        emit_sales_signal(session, ticket.npi, signal, value=intent, when=when)
    session.flush()
    METRICS.incr("support_classified", intent=intent)
    emit(_LOG, "support_handled", ticket_id=ticket.id, npi=ticket.npi, intent=intent,
         status=status, sales_signal=signal)
    return SupportOutcome(intent=intent, status=status, sales_signal=signal, escalated=(status == "escalated"))


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
