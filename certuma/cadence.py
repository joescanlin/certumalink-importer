"""The cadence engine (Phase 2 task P2.4) - multi-step follow-ups.

A first touch is no longer the end. run_cadence finds leads whose next follow-up is due
(next_action_at <= now) and, for each, sends the next touch through the deterministic SENDER (so
the FULL Gate runs every time), schedules the one after it, and gives up after the last touch:

  awaiting_reply / interested, due, not suppressed:
    cadence_step >= MAX_STEP   -> exhausted (no reply / no claim after every touch)
    otherwise                  -> draft the next touch, bump cadence_step (a fresh idempotency key,
                                  so the at-most-once guarantee holds per step), send, reschedule

An `interested` lead (a positive reply that has not claimed yet) is a first-class cadence state: it
gets nudged with the claim link, the same way, until it claims (the poller converts it) or runs out.
Suppression / a reply / activation between ticks naturally stops the lead (it is no longer in a
cadence state, or is_suppressed skips it).

Follow-ups auto-send here; whether a given campaign's cadence runs autonomously or is proposed for
approval is the autonomy policy's call (P2.6), which decides whether to invoke this engine.
Caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma import gate, ledger_writer
from certuma.config import Settings, get_settings
from certuma.copywriter import draft_email
from certuma.db.models import Lead
from certuma.observability import METRICS, emit, get_logger
from certuma.orchestrator import pick_contact, pick_mailbox
from certuma.sender import send_one
from certuma_core.cadence import MAX_STEP, is_final_step, next_action_after
from certuma_core.status import IllegalTransition

__all__ = ["CadenceSummary", "run_cadence", "CADENCE_STATES", "HOLD_RETRY_HOURS"]

_LOG = get_logger("certuma.cadence")

CADENCE_STATES = ("awaiting_reply", "interested")
HOLD_RETRY_HOURS = 6  # re-check a transiently gated lead (quiet hours / cap) this soon (provisional)


@dataclass
class CadenceSummary:
    due: int = 0
    sent: int = 0
    exhausted: int = 0
    held: int = 0
    skipped: int = 0
    sent_npis: List[str] = field(default_factory=list)


def run_cadence(
    session: Session,
    *,
    copy_provider,
    email_provider,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
    limit: int = 200,
) -> CadenceSummary:
    """Send the next due follow-up for each lead whose cadence has come due. Caller commits."""
    settings = settings or get_settings()
    when = when or datetime.now(timezone.utc)
    leads = session.execute(
        select(Lead).where(
            Lead.activation_status.in_(CADENCE_STATES),
            Lead.next_action_at.isnot(None), Lead.next_action_at <= when,
        ).order_by(Lead.next_action_at).limit(limit)
    ).scalars().all()

    summary = CadenceSummary()
    for lead in leads:
        summary.due += 1

        if gate.is_suppressed(session, npi=lead.npi):
            lead.next_action_at = None
            summary.skipped += 1
            continue

        if is_final_step(lead.cadence_step):
            ledger_writer.transition(session, lead.id, "exhausted", actor="cadence",
                                     reason_code="cadence_exhausted", expected_version=lead.version)
            lead.next_action_at = None
            summary.exhausted += 1
            continue

        draft = draft_email(session, lead, provider=copy_provider, settings=settings)
        if not draft.ok:
            if draft.reason == "lint_failed":
                try:
                    ledger_writer.transition(session, lead.id, "needs_review", actor="cadence",
                                             reason_code="cadence_lint_failed", expected_version=lead.version)
                except IllegalTransition:
                    pass
            lead.next_action_at = None
            summary.skipped += 1
            continue

        contact = pick_contact(session, lead.npi)
        mailbox = pick_mailbox(session)
        if contact is None or not contact.email or mailbox is None:
            lead.next_action_at = None
            summary.skipped += 1
            continue

        # preview the Gate so a transient HOLD does not consume a cadence step
        decision = gate.evaluate(session, npi=lead.npi, email=contact.email, campaign=lead.campaign,
                                 when=when, mailbox=mailbox, settings=settings)
        if not decision.allowed:
            if decision.decision == gate.BLOCK:
                lead.next_action_at = None
                summary.skipped += 1
            else:  # HOLD: quiet hours / warmup / breaker -> retry soon, no step consumed
                lead.next_action_at = when + timedelta(hours=HOLD_RETRY_HOURS)
                summary.held += 1
            continue

        new_step = lead.cadence_step + 1
        lead.cadence_step = new_step  # bump BEFORE send: a fresh (npi,campaign,cadence_step) key
        outcome = send_one(session, lead, mailbox=mailbox, to_email=contact.email,
                           rendered=draft.rendered, provider=email_provider, settings=settings, when=when)
        if outcome.sent:
            lead.next_action_at = next_action_after(new_step, when)
            summary.sent += 1
            summary.sent_npis.append(lead.npi)
            METRICS.incr("cadence_sent", step=str(new_step))
            emit(_LOG, "cadence_sent", lead_id=lead.id, npi=lead.npi, step=new_step)
        else:  # the in-send Gate re-check disagreed with the preview (rare); do not retry this tick
            lead.next_action_at = (None if outcome.terminal else when + timedelta(hours=HOLD_RETRY_HOURS))
            summary.held += 0 if outcome.terminal else 1
            summary.skipped += 1 if outcome.terminal else 0

    session.flush()
    METRICS.incr("cadence_run")
    emit(_LOG, "cadence_run", due=summary.due, sent=summary.sent, exhausted=summary.exhausted,
         held=summary.held, skipped=summary.skipped)
    return summary
