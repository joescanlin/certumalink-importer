"""The deterministic SENDER (Phase 1 task P1.4).

send_one drives one sendable lead to email_sent with at-most-once semantics:

  gate.evaluate (ALLOW required) -> ensure Thread (random reply_token) -> ledger_writer.transition
  (which inserts the idempotency Message + flips status, BEFORE the ESP call) -> provider.send ->
  back-fill esp_message_id/sent_at.

The Message (idempotency key (npi, campaign, cadence_step) on the partial unique index) is written
and flushed BEFORE provider.send, inside one uncommitted transaction the caller owns. If send
raises, the caller rolls back and the Message+transition vanish together (key freed, safe retry).
A HOLD/BLOCK returns sent=False and performs no transition.

Trust boundary: the SENDER does NOT run the full copy linter (certuma_core.linter) - the render
step / copywriter (P1.8) is responsible for linting and passes an already-linted RenderedEmail.
The SENDER does perform a cheap last-line CAN-SPAM PRESENCE guard (the unsubscribe URL and the
postal address must actually appear in the rendered body) so a render bug can never send a
non-compliant email. Disposition of a HOLD (requeue) vs BLOCK (terminal, e.g. suppression) is the
caller's responsibility (P1.11); see SendOutcome.terminal.

Optimistic-concurrency / stale-version rejection is enforced by ledger_writer.transition and tested
in tests/db/test_ledger_writer.py; send_one threads lead.version through as expected_version.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma import gate, ledger_writer
from certuma.config import Settings, get_settings
from certuma.db.models import Lead, Message, Thread
from certuma.email import build_outbound
from certuma.observability import METRICS, emit, get_logger

__all__ = ["RenderedEmail", "SendOutcome", "ensure_thread", "send_one"]

_LOG = get_logger("certuma.sender")


@dataclass(frozen=True)
class RenderedEmail:
    """A fully-rendered, pre-linted email (the render step / copywriter produces this)."""
    subject: str
    body: str          # html
    plaintext: str
    variant_id: str
    unsubscribe_url: str
    unsubscribe_mailto: str


@dataclass(frozen=True)
class SendOutcome:
    sent: bool
    decision: Optional[gate.GateDecision] = None
    message_id: Optional[int] = None
    esp_message_id: Optional[str] = None

    @property
    def terminal(self) -> bool:
        """True when the lead should be STOPPED, not requeued (a BLOCK, e.g. suppression)."""
        return self.decision is not None and self.decision.decision == gate.BLOCK


def _assert_compliant(rendered: RenderedEmail, settings: Settings) -> None:
    """Cheap last-line CAN-SPAM presence guard on the rendered body (a render bug must not send)."""
    blob = f"{rendered.body or ''}\n{rendered.plaintext or ''}"
    if not rendered.unsubscribe_url or rendered.unsubscribe_url not in blob:
        raise ValueError("rendered email missing the unsubscribe url in body/plaintext")
    if settings.postal_address and settings.postal_address not in blob:
        raise ValueError("rendered email missing the postal address in body/plaintext")


def ensure_thread(session: Session, lead_id: int) -> Thread:
    thread = session.execute(select(Thread).where(Thread.lead_id == lead_id)).scalar()
    if thread is None:
        thread = Thread(lead_id=lead_id, reply_token=secrets.token_urlsafe(16))
        session.add(thread)
        session.flush()
    return thread


def send_one(
    session: Session,
    lead: Lead,
    *,
    mailbox,
    to_email: str,
    rendered: RenderedEmail,
    provider,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
) -> SendOutcome:
    """Send one email for a sendable lead. Caller owns the transaction (commit on success)."""
    settings = settings or get_settings()
    when = when or datetime.now(timezone.utc)

    decision = gate.evaluate(
        session, npi=lead.npi, email=to_email, campaign=lead.campaign,
        when=when, mailbox=mailbox, settings=settings,
    )
    if not decision.allowed:
        METRICS.incr("send_gated", reason=decision.reason_code or "")
        emit(_LOG, "send_gated", lead_id=lead.id, decision=decision.decision, reason_code=decision.reason_code)
        return SendOutcome(sent=False, decision=decision)

    _assert_compliant(rendered, settings)  # last-line guard before any state change

    thread = ensure_thread(session, lead.id)
    reply_domain = settings.reply_to_domain or settings.cold_domain or "localhost"
    reply_to = f"reply+{thread.reply_token}@{reply_domain}"

    outbound = build_outbound(
        to_addr=to_email, from_addr=settings.sender_from_email, from_name=settings.sender_from_name,
        subject=rendered.subject, html_body=rendered.body, text_body=rendered.plaintext,
        reply_to=reply_to, unsubscribe_url=rendered.unsubscribe_url, unsubscribe_mailto=rendered.unsubscribe_mailto,
    )

    # idempotency Message written + flushed inside transition(), BEFORE the ESP call
    idempotency = dict(
        lead_id=lead.id, thread_id=thread.id, mailbox_id=mailbox.id, npi=lead.npi,
        campaign=lead.campaign, cadence_step=lead.cadence_step, direction="outbound",
        subject=rendered.subject, body_rendered=rendered.body, variant_id=rendered.variant_id,
    )
    ledger_writer.transition(
        session, lead.id, "email_sent", actor="sender", reason_code="send",
        expected_version=lead.version, idempotency=idempotency,
    )

    # the ESP call. If this raises, the caller rolls back: Message + transition undone together.
    result = provider.send(outbound)

    # back-fill via the exact idempotency unique key (npi, campaign, cadence_step) WHERE outbound,
    # so scalar_one is guaranteed unambiguous even if sibling outbound rows existed.
    message = session.execute(
        select(Message).where(
            Message.npi == lead.npi, Message.campaign == lead.campaign,
            Message.cadence_step == lead.cadence_step, Message.direction == "outbound",
        )
    ).scalar_one()
    message.esp_message_id = result.provider_message_id
    message.sent_at = when
    session.flush()

    METRICS.incr("send_ok")
    emit(_LOG, "email_sent", lead_id=lead.id, npi=lead.npi, mailbox_id=mailbox.id,
         esp_message_id=result.provider_message_id)
    return SendOutcome(sent=True, message_id=message.id, esp_message_id=result.provider_message_id)
