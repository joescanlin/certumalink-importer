"""Inbound reply ingestion (Phase 2 task P2.1).

The outbound SENDER sets a plus-addressed Reply-To (`reply+<token>@domain`) whose token maps to a
Thread, hence a Lead. ingest_reply takes a normalized inbound reply (token + text + provider
message id), threads it back, stores it as a deduped inbound Message, moves the lead
email_sent|awaiting_reply -> replied, and emits a `replied` Event. handle_reply then runs the
classifier (P2.2) to resolve the reply.

Dedup rides the existing uq_msg_inbound_esp unique index (one inbound row per provider message id),
so a webhook redelivery or an overlapping poll is a no-op. There is no real inbound mail transport
in dev (Mailpit is outbound-only); this is the normalized seam a simulator (or the future
IMAP/ESP-webhook adapter) feeds. Caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from certuma import monitor
from certuma.classifier import ClassifyOutcome, classify_reply
from certuma.db.models import Lead, Message, Thread
from certuma.observability import METRICS, emit, get_logger

__all__ = ["InboundResult", "ingest_reply", "handle_reply"]

_LOG = get_logger("certuma.inbound")


@dataclass(frozen=True)
class InboundResult:
    matched: bool                       # did the reply token map to a known thread/lead?
    duplicate: bool = False             # already ingested this provider message id?
    lead_id: Optional[int] = None
    message_id: Optional[int] = None
    transitioned_to: Optional[str] = None


def _now(when: Optional[datetime]) -> datetime:
    return when or datetime.now(timezone.utc)


def ingest_reply(
    session: Session,
    *,
    reply_token: str,
    text: str,
    esp_message_id: str,
    occurred_at: datetime,
    from_email: Optional[str] = None,
    when: Optional[datetime] = None,
) -> InboundResult:
    """Thread + store one inbound reply, move the lead to `replied`, emit a `replied` Event."""
    when = _now(when)
    thread = session.execute(select(Thread).where(Thread.reply_token == reply_token)).scalar()
    if thread is None:
        METRICS.incr("inbound_unmatched")
        emit(_LOG, "inbound_unmatched", reply_token=reply_token, esp_message_id=esp_message_id)
        return InboundResult(matched=False)

    lead = session.get(Lead, thread.lead_id)
    in_reply_to = session.execute(
        select(Message.id).where(Message.lead_id == lead.id, Message.direction == "outbound")
        .order_by(Message.id.desc()).limit(1)
    ).scalar()

    try:
        with session.begin_nested():
            msg = Message(
                lead_id=lead.id, thread_id=thread.id, npi=lead.npi, campaign=lead.campaign,
                cadence_step=lead.cadence_step, direction="inbound", body_rendered=text,
                esp_message_id=esp_message_id, in_reply_to=in_reply_to, sent_at=occurred_at,
            )
            session.add(msg)
            session.flush()
    except IntegrityError:
        METRICS.incr("inbound_duplicate")
        return InboundResult(matched=True, duplicate=True, lead_id=lead.id)

    lead.last_engaged_at = when  # a reply is the strongest engagement signal (P3.5 rollup)
    moved = monitor.try_transition(session, lead, "replied", actor="inbound", reason_code="reply_received")
    monitor.record_event(session, event_type="replied", dedup_key=f"reply:{esp_message_id}",
                         occurred_at=occurred_at, lead_id=lead.id, message_id=msg.id, npi=lead.npi,
                         payload={"from": from_email or ""})
    session.flush()
    METRICS.incr("inbound_ingested")
    emit(_LOG, "inbound_ingested", lead_id=lead.id, npi=lead.npi, message_id=msg.id,
         to_replied=moved)
    return InboundResult(matched=True, duplicate=False, lead_id=lead.id, message_id=msg.id,
                         transitioned_to=("replied" if moved else None))


def handle_reply(
    session: Session,
    *,
    reply_token: str,
    text: str,
    esp_message_id: str,
    occurred_at: datetime,
    from_email: Optional[str] = None,
    classifier=None,
    when: Optional[datetime] = None,
) -> Tuple[InboundResult, Optional[ClassifyOutcome]]:
    """Ingest a reply and immediately classify it. Returns (ingest result, classify outcome)."""
    res = ingest_reply(session, reply_token=reply_token, text=text, esp_message_id=esp_message_id,
                       occurred_at=occurred_at, from_email=from_email, when=when)
    if not res.matched or res.duplicate:
        return res, None

    lead = session.get(Lead, res.lead_id)
    message = session.get(Message, res.message_id)
    # context for the classifier: the subject of the outbound it answers
    context = ""
    if message.in_reply_to is not None:
        out = session.get(Message, message.in_reply_to)
        context = (out.subject or "") if out is not None else ""
    outcome = classify_reply(session, lead, message, provider=classifier, when=when, context=context)
    return res, outcome
