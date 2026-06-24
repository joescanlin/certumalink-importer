"""The Assisted-loop orchestrator (Phase 1 task P1.11).

This is the seam between the autonomous machinery and the one human in the loop. In Phase 1 every
send is Assisted: the orchestrator PROPOSES (drafts the copy a human will actually read) and the
human DISPOSES (approves on the dashboard), after which the orchestrator EXECUTES the exact
reviewed draft. Three entry points:

  propose_sends        for each sendable lead on an active campaign, draft via the COPYWRITER and
                       file a pending Approval carrying the reviewed subject/body + an SLA clock.
                       A lint failure routes the lead to needs_review; a suppressed lead or one
                       already awaiting a decision is skipped. (No lead is sent here.)
  execute_approved_send  on an approved Approval, reconstruct the byte-identical RenderedEmail the
                       human saw (the compliance tokens were already injected at draft time; the
                       List-Unsubscribe URL is re-derived deterministically) and hand it to the
                       deterministic SENDER, which re-runs the FULL Gate before any send.
  expire_stale_approvals  flip pending Approvals past their SLA to expired (escalation signal).

What the human approves is exactly what is sent: the draft is generated ONCE at propose time and
the stored subject/body are replayed verbatim, so a non-deterministic model cannot swap the copy
between review and send.

Autonomy policy is read from the campaign (Phase 1: only 'assisted' is wired; supervised/autonomous
are Phase 2). value_tier mirrors the workflow_score activation_priority. The SLA window and the
mailbox-selection strategy are deliberately simple here and called out as provisional.
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
from certuma.db.models import Approval, Campaign, Contact, Lead, Mailbox, WorkflowScore
from certuma.observability import METRICS, emit, get_logger
from certuma.sender import RenderedEmail, SendOutcome, send_one
from certuma_core import urls
from certuma_core.status import IllegalTransition

__all__ = [
    "OrchestratorError", "NotApproved", "NoValidContact", "NoMailbox",
    "ProposeResult", "propose_sends", "execute_approved_send", "expire_stale_approvals",
    "pick_contact", "pick_mailbox", "DEFAULT_SLA_HOURS",
]

_LOG = get_logger("certuma.orchestrator")

# Provisional Assisted-mode SLA: how long a proposal waits for a human before it is escalated.
DEFAULT_SLA_HOURS = 24


class OrchestratorError(RuntimeError):
    """Base class for orchestrator precondition failures."""


class NotApproved(OrchestratorError):
    """execute_approved_send called on an Approval that is not in the 'approved' state."""


class NoValidContact(OrchestratorError):
    """The lead has no deliverable (email_status='valid') contact to send to."""


class NoMailbox(OrchestratorError):
    """No active sending mailbox is configured."""


@dataclass
class ProposeResult:
    proposed: int = 0
    skipped: int = 0
    needs_review: int = 0
    approval_ids: List[int] = field(default_factory=list)


def _now(when: Optional[datetime]) -> datetime:
    return when or datetime.now(timezone.utc)


def pick_contact(session: Session, npi: str) -> Optional[Contact]:
    """The best deliverable contact for a prospect: valid status, real mailbox preferred over role."""
    return session.execute(
        select(Contact).where(Contact.npi == npi, Contact.email_status == "valid")
        .order_by(Contact.is_role_address.asc(), Contact.id.desc()).limit(1)
    ).scalar()


def pick_mailbox(session: Session) -> Optional[Mailbox]:
    """An active sending mailbox. Phase 1 runs a single dev mailbox; warmup balancing is Phase 2."""
    return session.execute(
        select(Mailbox).where(Mailbox.is_active.is_(True)).order_by(Mailbox.id).limit(1)
    ).scalar()


def _value_tier(session: Session, npi: str) -> Optional[str]:
    return session.execute(
        select(WorkflowScore.activation_priority).where(WorkflowScore.npi == npi)
        .order_by(WorkflowScore.scored_at.desc()).limit(1)
    ).scalar()


def _active_campaigns(session: Session) -> set:
    return set(
        session.execute(
            select(Campaign.name).where(Campaign.is_active.is_(True), Campaign.is_paused.is_(False))
        ).scalars().all()
    )


def propose_sends(
    session: Session,
    *,
    provider,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
    limit: int = 200,
    sla_hours: int = DEFAULT_SLA_HOURS,
) -> ProposeResult:
    """Draft + file a pending Approval for each sendable lead on an active campaign. Caller commits."""
    settings = settings or get_settings()
    when = _now(when)
    sla = when + timedelta(hours=sla_hours)
    active = _active_campaigns(session)

    pending = select(Approval.lead_id).where(Approval.state == "pending")
    leads = session.execute(
        select(Lead).where(Lead.activation_status == "sendable", Lead.id.notin_(pending))
        .order_by(Lead.id).limit(limit)
    ).scalars().all()

    result = ProposeResult()
    for lead in leads:
        if lead.campaign not in active:
            result.skipped += 1
            continue
        if gate.is_suppressed(session, npi=lead.npi):
            result.skipped += 1
            continue

        draft = draft_email(session, lead, provider=provider, settings=settings)
        if not draft.ok:
            if draft.reason == "lint_failed":
                try:
                    ledger_writer.transition(
                        session, lead.id, "needs_review", actor="copywriter",
                        reason_code="lint_failed", expected_version=lead.version,
                    )
                    result.needs_review += 1
                except IllegalTransition:
                    result.skipped += 1
            else:  # no_approved_template etc: leave the lead sendable and wait
                result.skipped += 1
            continue

        approval = Approval(
            lead_id=lead.id, proposed_action="send_email",
            value_tier=_value_tier(session, lead.npi),
            proposed_subject=draft.rendered.subject, proposed_body=draft.rendered.body,
            state="pending", sla_expires_at=sla,
        )
        session.add(approval)
        session.flush()
        result.proposed += 1
        result.approval_ids.append(approval.id)
        METRICS.incr("approval_proposed")
        emit(_LOG, "approval_proposed", approval_id=approval.id, lead_id=lead.id,
             npi=lead.npi, value_tier=approval.value_tier, model=draft.model)

    return result


def execute_approved_send(
    session: Session,
    approval: Approval,
    *,
    provider_email,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
) -> SendOutcome:
    """Send the exact draft a human approved. Caller owns the transaction (commit on success)."""
    settings = settings or get_settings()
    when = _now(when)
    if approval.state != "approved":
        raise NotApproved(f"approval {approval.id} is {approval.state!r}, not 'approved'")

    lead = session.get(Lead, approval.lead_id)
    contact = pick_contact(session, lead.npi)
    if contact is None or not contact.email:
        raise NoValidContact(f"lead {lead.id} (npi {lead.npi}) has no valid contact")
    mailbox = pick_mailbox(session)
    if mailbox is None:
        raise NoMailbox("no active sending mailbox configured")

    domain = settings.cold_domain or "localhost"
    rendered = RenderedEmail(
        subject=approval.proposed_subject or "",
        body=approval.proposed_body or "",
        plaintext=approval.proposed_body or "",
        variant_id="approved",
        unsubscribe_url=urls.unsubscribe_url(domain, lead.npi),
        unsubscribe_mailto=urls.unsubscribe_mailto(domain),
    )
    outcome = send_one(
        session, lead, mailbox=mailbox, to_email=contact.email, rendered=rendered,
        provider=provider_email, settings=settings, when=when,
    )
    METRICS.incr("approved_send", sent=str(outcome.sent).lower())
    emit(_LOG, "approved_send", approval_id=approval.id, lead_id=lead.id, npi=lead.npi,
         sent=outcome.sent, reason_code=(outcome.decision.reason_code if outcome.decision else None))
    return outcome


def expire_stale_approvals(session: Session, *, when: Optional[datetime] = None) -> int:
    """Flip pending Approvals past their SLA to 'expired' (a human did not act in time). Caller commits."""
    when = _now(when)
    stale = session.execute(
        select(Approval).where(Approval.state == "pending", Approval.sla_expires_at < when)
    ).scalars().all()
    for approval in stale:
        approval.state = "expired"
        approval.decided_at = when
        METRICS.incr("approval_expired")
        emit(_LOG, "approval_expired", approval_id=approval.id, lead_id=approval.lead_id)
    session.flush()
    return len(stale)
