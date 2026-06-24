"""End-to-end Assisted-loop demo (Phase 1 tasks P1.5 hand-seed + P1.12 demo).

Proves the whole Certuma Reach loop on the smallest real slice:

  seed_demo   active campaign + approved template + prospect + a hand-seeded VALID contact (the
              enrichment output P1.5 will eventually produce) + an active mailbox + a sendable lead
              carrying a claim_url.
  run_demo    propose (COPYWRITER drafts, a pending Approval is filed) -> approve -> execute
              (Gate -> SENDER, a real email) -> delivered event (-> awaiting_reply) -> poll the
              claim_url (the physician "clicked") -> physician_activated.

Both halves are reusable. tests/db/test_demo_e2e.py drives them deterministically (stub copy +
capture email + a fixed business-hours clock). `python -m certuma.demo` drives them against the
live DB and the isolated certuma-mailpit, sending a real email you can open in the Mailpit UI.

The claim-status source does not exist yet (certuma.publish.claim_status is a stub), so the demo
injects a fetch that reports the lead as claimed - simulating the physician clicking the link.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from certuma import monitor, orchestrator, poller
from certuma.config import Settings, get_settings
from certuma.copywriter import StubCopyProvider
from certuma.db.models import (Approval, Campaign, Contact, Event, Lead, Mailbox, Message,
                               Prospect, Suppression, Template, Thread, WorkflowScore)

__all__ = ["DemoReport", "DEMO_NPI", "DEMO_CAMPAIGN", "seed_demo", "reset_demo", "run_demo", "main"]

DEMO_NPI = "1999999999"
DEMO_CAMPAIGN = "dermatology"
DEMO_CLAIM_URL = "https://www.certumalink.com/claim/demo-derm"
# A weekday business-hours instant (Tue 10:00 CDT) so the Gate's quiet-hours check passes
# deterministically regardless of the wall clock the demo is run at.
DEMO_WHEN = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)


@dataclass
class DemoReport:
    proposed: int = 0
    approval_id: Optional[int] = None
    subject: str = ""
    sent: bool = False
    esp_message_id: Optional[str] = None
    to_email: str = ""
    delivered_to: Optional[str] = None   # lead status after the delivered event
    activated: bool = False
    final_status: str = ""

    def lines(self) -> list:
        return [
            f"proposed approvals : {self.proposed}",
            f"approval id        : {self.approval_id}",
            f"drafted subject    : {self.subject}",
            f"sent               : {self.sent}  (to {self.to_email})",
            f"esp message id     : {self.esp_message_id}",
            f"after delivered    : {self.delivered_to}",
            f"claim activated    : {self.activated}",
            f"final lead status  : {self.final_status}",
        ]


def reset_demo(session: Session, *, npi: str = DEMO_NPI, mailbox_address: str = "") -> None:
    """Delete any prior demo rows so the demo is re-runnable. FK-safe order. Caller commits."""
    lead_ids = select(Lead.id).where(Lead.npi == npi)
    session.execute(delete(Event).where((Event.npi == npi) | (Event.lead_id.in_(lead_ids))))
    session.execute(delete(Message).where((Message.npi == npi) | (Message.lead_id.in_(lead_ids))))
    session.execute(delete(Approval).where(Approval.lead_id.in_(lead_ids)))
    session.execute(delete(Thread).where(Thread.lead_id.in_(lead_ids)))
    session.execute(delete(Lead).where(Lead.npi == npi))
    session.execute(delete(Contact).where(Contact.npi == npi))
    session.execute(delete(Suppression).where(Suppression.npi == npi))
    session.execute(delete(WorkflowScore).where(WorkflowScore.npi == npi))
    session.execute(delete(Prospect).where(Prospect.npi == npi))
    if mailbox_address:
        session.execute(delete(Mailbox).where(Mailbox.address == mailbox_address))
    session.flush()


def seed_demo(
    session: Session,
    *,
    settings: Optional[Settings] = None,
    npi: str = DEMO_NPI,
    mailbox_address: str = "",
    last_name: str = "Avery",
) -> Lead:
    """Seed the smallest sendable slice and return the demo lead. Caller commits."""
    settings = settings or get_settings()
    mailbox_address = mailbox_address or settings.sender_from_email or "jordan@getcertuma.com"
    reset_demo(session, npi=npi, mailbox_address=mailbox_address)

    # an active campaign with an approved (campaign-agnostic) compliant template
    session.execute(update(Campaign).where(Campaign.name == DEMO_CAMPAIGN)
                    .values(is_active=True, is_paused=False))
    session.execute(update(Template).where(Template.campaign.is_(None), Template.version == 1)
                    .values(is_approved=True, approved_by="demo"))

    session.add(Prospect(npi=npi, first_name="Jordan", last_name=last_name,
                         display_name=f"Jordan {last_name} MD", credential="MD",
                         primary_specialty="Dermatology", practice_city="Austin", practice_state="TX"))
    session.flush()
    session.add(WorkflowScore(npi=npi, campaign="", activation_priority="high", activation_score=82,
                              profile_completeness_score=100, practice_group_size=5, model_version="demo"))
    # the hand-seeded valid contact (stands in for the P1.5 enrichment output)
    session.add(Contact(npi=npi, email=f"jordan.{last_name.lower()}@example.com", email_status="valid",
                        discovery_source="hand_seed"))
    session.add(Mailbox(address=mailbox_address, display_name=settings.sender_from_name or "Jordan Avery",
                        domain=settings.cold_domain or "getcertuma.com", is_active=True))
    lead = Lead(npi=npi, campaign=DEMO_CAMPAIGN, activation_status="sendable", claim_url=DEMO_CLAIM_URL)
    session.add(lead)
    session.flush()
    return lead


def run_demo(
    session: Session,
    *,
    npi: str = DEMO_NPI,
    settings: Optional[Settings] = None,
    copy_provider=None,
    email_provider,
    when: Optional[datetime] = None,
    claim_fetch: Callable[[str], str] = lambda url: "claimed",
) -> DemoReport:
    """Drive propose -> approve -> send -> delivered -> poll-activate for the demo lead. Caller commits."""
    settings = settings or get_settings()
    copy_provider = copy_provider or StubCopyProvider()
    when = when or DEMO_WHEN
    report = DemoReport()

    # 1. propose: the copywriter drafts and a pending Approval is filed for human review
    proposal = orchestrator.propose_sends(session, provider=copy_provider, settings=settings, when=when)
    report.proposed = proposal.proposed
    approval = session.execute(
        select(Approval).join(Lead, Approval.lead_id == Lead.id)
        .where(Lead.npi == npi, Approval.state == "pending").order_by(Approval.id.desc()).limit(1)
    ).scalar()
    if approval is None:
        return report
    report.approval_id = approval.id
    report.subject = approval.proposed_subject or ""

    # 2. the human approves on the dashboard
    approval.state = "approved"
    approval.decided_at = when
    session.flush()

    # 3. execute: Gate -> SENDER -> a real email
    outcome = orchestrator.execute_approved_send(
        session, approval, provider_email=email_provider, settings=settings, when=when)
    report.sent = outcome.sent
    report.esp_message_id = outcome.esp_message_id
    lead = session.get(Lead, approval.lead_id)
    contact = orchestrator.pick_contact(session, npi)
    report.to_email = contact.email if contact else ""
    if not outcome.sent:
        report.final_status = lead.activation_status
        return report

    # 4. delivered event -> awaiting_reply
    msg = session.execute(
        select(Message).where(Message.lead_id == lead.id, Message.direction == "outbound")
    ).scalar_one()
    monitor.ingest_event(session, event_type="delivered", dedup_key=f"demo-delivered-{msg.id}",
                         occurred_at=when, message_id=msg.id)
    session.refresh(lead)
    report.delivered_to = lead.activation_status

    # 5. the physician clicks the claim link; the poller converts -> physician_activated
    summary = poller.poll_once(session, fetch=claim_fetch, when=when)
    report.activated = summary.activated > 0
    session.refresh(lead)
    report.final_status = lead.activation_status
    return report


def main(argv: Optional[list] = None) -> int:
    """Run the demo live: real DB + the isolated Mailpit + a real SMTP send.

    By default the DB work runs in ONE transaction that is rolled back at the end, so the demo
    never pollutes a shared dev DB (the test suite assumes a clean migrated baseline). The email is
    still really delivered (SMTP is not transactional, so the Mailpit message persists) and the
    console report is computed before rollback, so you see the whole loop. Pass --commit to persist
    the seeded prospect/lead/approval so you can also inspect them in the dashboard.
    """
    import sys

    from certuma.db.session import make_engine
    from certuma.email import MailpitProvider

    commit = "--commit" in (argv if argv is not None else sys.argv[1:])
    settings = get_settings()
    smtp_host = settings.smtp_host or "127.0.0.1"
    smtp_port = int(os.environ.get("CERTUMA_MAILPIT_SMTP_PORT", "11026"))
    mailpit_ui = os.environ.get("CERTUMA_MAILPIT_UI", "http://127.0.0.1:18026")
    # ensure compliant-config defaults so the live Gate ALLOWs
    if not settings.postal_address:
        settings = Settings(**{**settings.__dict__, "postal_address": "Certuma, 1 Main St, Austin TX 78701"})
    if not settings.sender_from_email:
        settings = Settings(**{**settings.__dict__, "sender_from_email": "jordan@getcertuma.com",
                               "sender_from_name": "Jordan Avery", "cold_domain": "getcertuma.com"})

    engine = make_engine(settings)
    provider = MailpitProvider(smtp_host, smtp_port)
    with Session(engine) as session:
        seed_demo(session, settings=settings)
        report = run_demo(session, settings=settings, email_provider=provider)
        if commit:
            session.commit()
        else:
            session.rollback()

    print("\n=== Certuma Reach end-to-end demo ===")
    for line in report.lines():
        print("  " + line)
    print(f"\n  open the sent email in Mailpit: {mailpit_ui}")
    print("  DB:", "committed (visible in the dashboard)" if commit
          else "rolled back (email still delivered; pass --commit to persist)")
    ok = report.sent and report.final_status == "physician_activated"
    print("\n  RESULT:", "OK - full loop send -> delivered -> activated" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
