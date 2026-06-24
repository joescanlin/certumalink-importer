"""End-to-end PARITY demo (Phase 3 task P3.11).

Proves the whole Phase-3 stack on one slice, the way demo.py proved Phase 1: from raw prospects
(no contacts) all the way to governed evidence.

  signals      collect the knowledge-graph signals (license, board, group, message-burden, EHR...)
  tick #1      enrich (discovery + verify -> a real contact) -> propose -> AUTO-SEND on autopilot,
               A/B-assigning a template variant per clinician
  engagement   delivered + opened events, then replies (one interested, one objection)
  tick #2      the claim poller converts the interested lead -> physician_activated
  evidence     rebuild the reporting schema; the funnel, the winning variant, the engagement plays,
               and the governed export all reflect the run

Deterministic stubs throughout (stub copy, capture email, stub classifier, stub enrichment/signals,
a simulated claim). tests/db/test_parity_e2e.py asserts the chain; run_parity also backs a live run.
Caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, select, update

from certuma import (engagement, inbound, learning, monitor, reporting, scheduler, signals)
from certuma.classifier import StubReplyClassifier
from certuma.config import Settings, get_settings
from certuma.copywriter import StubCopyProvider
from certuma.db.models import (Campaign, Contact, Event, Lead, Mailbox, Message, PracticeGroup,
                               Prospect, Suppression, Template, Thread, WorkflowScore, ClinicianSignal)
from certuma.enrichment import StubEnrichProvider, StubVerifyProvider
from certuma.reporting import queries as rq
from certuma.reporting.export import MemoryExporter, export_evidence

__all__ = ["ParityReport", "PARITY_DOCS", "seed_parity", "run_parity", "PARITY_WHEN", "main"]

PARITY_WHEN = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)
_BODY = ("Hi Dr. {last_name}, your {pitch_angle} in {city}. Review your profile: {claim_url}. "
         "Unsubscribe: {unsubscribe_url}. {postal_address}")

# npi, first, last, specialty, group_size
PARITY_DOCS = [
    ("1980000201", "Mara", "Singh", "Dermatology", 8),
    ("1980000202", "Liam", "Ortega", "Dermatology", 3),
    ("1980000203", "Priya", "Nguyen", "Cardiology", 12),
    ("1980000204", "Evan", "Brooks", "Cardiology", 2),
]


@dataclass
class ParityReport:
    signals: int = 0
    enriched: int = 0
    auto_sent: int = 0
    opened: int = 0
    activated: int = 0
    winning_variant: Optional[str] = None
    engagement_plays: int = 0
    evidence_datasets: List[str] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)

    def lines(self) -> list:
        f = self.funnel or {}
        return [
            f"signals collected   : {self.signals}",
            f"enriched -> sendable: {self.enriched}",
            f"auto-sent (no human): {self.auto_sent}",
            f"opened              : {self.opened}",
            f"activated           : {self.activated}",
            f"winning variant     : {self.winning_variant}",
            f"engagement plays    : {self.engagement_plays}",
            f"funnel (sent/replied/activated): "
            f"{f.get('sent')}/{f.get('replied')}/{f.get('activated')}  "
            f"(open rate {f.get('open_rate')}%)",
            f"evidence datasets   : {', '.join(self.evidence_datasets)}",
        ]


def seed_parity(session, *, settings: Optional[Settings] = None) -> None:
    """Seed raw prospects (NO contacts) + two A/B template variants on an autonomous campaign."""
    settings = settings or get_settings()
    session.execute(update(Campaign).where(Campaign.name == "dermatology")
                    .values(is_active=True, is_paused=False, autonomy_level="autonomous"))
    session.execute(delete(Template).where(Template.campaign == "dermatology"))
    session.add(Template(campaign="dermatology", version=1, subject="Your profile is ready",
                         body=_BODY, variant_label="A", is_approved=True, approved_by="parity"))
    session.add(Template(campaign="dermatology", version=2, subject="A quick note on your profile",
                         body=_BODY, variant_label="B", is_approved=True, approved_by="parity"))
    session.execute(update(Mailbox).values(is_active=False))
    session.add(Mailbox(address=(settings.sender_from_email or "jordan@getcertuma.com"),
                        domain="getcertuma.com", is_active=True))
    session.flush()
    for npi, fn, ln, spec, grp in PARITY_DOCS:
        for tbl in (ClinicianSignal, Event, Message, Contact, WorkflowScore):
            session.execute(delete(tbl).where(tbl.npi == npi))
        session.execute(delete(Thread).where(Thread.lead_id.in_(select(Lead.id).where(Lead.npi == npi))))
        session.execute(delete(Lead).where(Lead.npi == npi))
        session.execute(delete(Prospect).where(Prospect.npi == npi))
        session.execute(delete(PracticeGroup).where(PracticeGroup.practice_group_id == f"pg{npi}"))
        session.add(PracticeGroup(practice_group_id=f"pg{npi}", practice_group_size=grp))
        session.flush()
        session.add(Prospect(npi=npi, first_name=fn, last_name=ln, display_name=f"{fn} {ln} MD",
                             credential="MD", primary_specialty=spec, practice_city="Austin",
                             practice_state="TX", practice_group_id=f"pg{npi}"))
        session.flush()
        session.add(WorkflowScore(npi=npi, campaign="", activation_priority="high", activation_score=80,
                                  profile_completeness_score=90, practice_group_size=grp, model_version="parity"))
        # NO contact: enrichment will find one. Lead starts not_contacted.
        session.add(Lead(npi=npi, campaign="dermatology", activation_status="not_contacted",
                         claim_url=f"https://www.certumalink.com/claim/{npi}"))
        session.flush()


def run_parity(session, *, settings: Optional[Settings] = None, copy_provider=None, email_provider,
               when: Optional[datetime] = None) -> ParityReport:
    """Drive the full Phase-3 loop end to end. Caller owns the transaction."""
    settings = settings or get_settings()
    copy_provider = copy_provider or StubCopyProvider()
    when = when or PARITY_WHEN
    report = ParityReport()

    # 1. knowledge-graph signals
    report.signals = signals.run_signal_collection(session, when=when).signals_written

    # 2. one tick: enrich (discovery+verify) -> propose -> AUTO-SEND (autonomous), A/B variants
    t1 = scheduler.tick(session, copy_provider=copy_provider, email_provider=email_provider,
                        settings=settings, when=when,
                        discovery=StubEnrichProvider(), verify=StubVerifyProvider())
    report.enriched, report.auto_sent = t1.enriched, t1.auto_sent

    # 3. engagement: deliver + open every auto-sent lead, then two replies
    sent = session.execute(select(Lead).where(Lead.activation_status == "email_sent")).scalars().all()
    for lead in sent:
        m = session.execute(select(Message).where(
            Message.lead_id == lead.id, Message.direction == "outbound").order_by(Message.id.desc())
        ).scalars().first()
        monitor.ingest_event(session, event_type="delivered", dedup_key=f"d-{m.id}",
                             occurred_at=when, message_id=m.id)
        monitor.ingest_event(session, event_type="opened", dedup_key=f"o-{lead.id}",
                             occurred_at=when, lead_id=lead.id, when=when)
        report.opened += 1

    def _reply(npi, text, mid):
        lead = session.execute(select(Lead).where(Lead.npi == npi)).scalar()
        thread = session.execute(select(Thread).where(Thread.lead_id == lead.id)).scalar() if lead else None
        if thread:
            inbound.handle_reply(session, reply_token=thread.reply_token, text=text, esp_message_id=mid,
                                 occurred_at=when, classifier=StubReplyClassifier(), when=when)

    _reply("1980000202", "Yes, I'd like to claim my profile", "rp-int")     # interested
    _reply("1980000204", "what does this cost? is this legit?", "rp-obj")    # objection -> escalate

    # 4. tick 2 with a claim source: the interested lead claims -> activated
    t2 = scheduler.tick(session, copy_provider=copy_provider, email_provider=email_provider,
                        settings=settings, when=when,
                        claim_fetch=lambda u: "claimed" if u.endswith("1980000202") else "pending")
    report.activated = t2.activated

    # 5. evidence: rebuild + read the analytics, the winner, the plays, the export
    reporting.rebuild(session, as_of=when)
    report.funnel = rq.funnel_totals(session)
    report.winning_variant = learning.winning_variant(session, campaign="dermatology", min_sample=1)
    report.engagement_plays = len(engagement.engagement_queue(session, now=when))
    report.evidence_datasets = export_evidence(session, exporter=MemoryExporter()).tables
    return report


def main(argv: Optional[list] = None) -> int:
    """Run the parity demo live (real DB + the isolated Mailpit). Defaults to a rolled-back
    transaction (emails still deliver to Mailpit); pass --commit to persist for the dashboard."""
    import os
    import sys

    from sqlalchemy.orm import Session as _Session

    from certuma.db.session import make_engine
    from certuma.email import MailpitProvider

    commit = "--commit" in (argv if argv is not None else sys.argv[1:])
    settings = get_settings()
    if not settings.postal_address:
        settings = Settings(**{**settings.__dict__, "postal_address": "Certuma, 1 Main St, Austin TX 78701"})
    if not settings.sender_from_email:
        settings = Settings(**{**settings.__dict__, "sender_from_email": "jordan@getcertuma.com",
                               "sender_from_name": "Jordan Avery", "cold_domain": "getcertuma.com"})
    smtp_port = int(os.environ.get("CERTUMA_MAILPIT_SMTP_PORT", "11026"))
    provider = MailpitProvider(settings.smtp_host or "127.0.0.1", smtp_port)

    engine = make_engine(settings)
    with _Session(engine) as session:
        seed_parity(session, settings=settings)
        report = run_parity(session, settings=settings, email_provider=provider)
        session.commit() if commit else session.rollback()

    print("\n=== Certuma Reach parity demo (Phase 3) ===")
    for line in report.lines():
        print("  " + line)
    print("  DB:", "committed (visible in the dashboard)" if commit
          else "rolled back (emails still delivered to Mailpit; pass --commit to persist)")
    ok = (report.activated >= 1 and report.winning_variant
          and "funnel_totals" in report.evidence_datasets)
    print("\n  RESULT:", "OK - signals + enrich + autopilot + engagement + learning + evidence"
          if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
