"""Autonomous end-to-end test (Phase 2 task P2.10). Skips without DB.

Proves the loop runs itself: a scheduler tick proposes AND auto-sends (no human) on an autonomous
campaign, inbound replies are classified into deterministic actions, and a claim-click activates -
all with zero approvals. Stub copy + capture email + stub classifier + a fixed business-hours clock.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, func, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import inbound, monitor, scheduler
    from certuma.classifier import StubReplyClassifier
    from certuma.config import Settings
    from certuma.copywriter import StubCopyProvider
    from certuma.db.models import (Approval, Campaign, Contact, Lead, Mailbox, Message, Prospect,
                                   Suppression, Template, Thread)
    from certuma.email.provider import SendResult

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    sender_from_title="Provider Onboarding", postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_SA else None
CLAIM1 = "https://www.certumalink.com/claim/auto-1"
CLAIM2 = "https://www.certumalink.com/claim/auto-2"


class CaptureEmailProvider:
    name = "capture"

    def __init__(self):
        self.count = 0

    def send(self, email):
        self.count += 1
        return SendResult(provider_message_id=f"esp-auto-{self.count}", accepted=True)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class AutonomousEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "lead" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")
        with Session(cls.engine) as s:
            if s.get(Campaign, "dermatology") is None:
                raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _prep(self, autonomy):
        self.session.execute(update(Template).where(Template.campaign.is_(None), Template.version == 1)
                             .values(is_approved=True))
        self.session.execute(update(Campaign).where(Campaign.name == "dermatology")
                             .values(is_active=True, is_paused=False, autonomy_level=autonomy))
        self.session.execute(update(Mailbox).values(is_active=False))
        self.session.add(Mailbox(address="auto@getcertuma.com", domain="getcertuma.com", is_active=True))
        self.session.flush()

    def _lead(self, npi, claim):
        self.session.add(Prospect(npi=npi, last_name="Auto", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        self.session.add(Contact(npi=npi, email=f"dr.{npi}@example.com", email_status="valid"))
        lead = Lead(npi=npi, campaign="dermatology", activation_status="sendable", claim_url=claim)
        self.session.add(lead)
        self.session.flush()
        return lead

    def _out_msg(self, lead):
        return self.session.execute(
            select(Message).where(Message.lead_id == lead.id, Message.direction == "outbound")
            .order_by(Message.id.desc()).limit(1)).scalar_one()

    def _thread(self, lead):
        return self.session.execute(select(Thread).where(Thread.lead_id == lead.id)).scalar_one()

    def test_autonomous_loop_runs_without_a_human(self):
        self._prep("autonomous")
        l1 = self._lead("2200000001", CLAIM1)
        l2 = self._lead("2200000002", CLAIM2)
        cap = CaptureEmailProvider()

        # --- tick 1: propose + auto-send both, no approvals ---
        r1 = scheduler.tick(self.session, copy_provider=StubCopyProvider(), email_provider=cap,
                            settings=SETTINGS, when=BUSINESS)
        self.assertEqual((r1.proposed, r1.auto_sent, r1.escalated), (2, 2, 0))
        self.session.refresh(l1)
        self.session.refresh(l2)
        self.assertEqual(l1.activation_status, "email_sent")
        self.assertEqual(l2.activation_status, "email_sent")
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Approval).where(Approval.state == "pending")).scalar(), 0)

        # --- delivery events -> awaiting_reply + first follow-up scheduled ---
        for lead in (l1, l2):
            m = self._out_msg(lead)
            monitor.ingest_event(self.session, event_type="delivered", dedup_key=f"d-{m.id}",
                                 occurred_at=BUSINESS, message_id=m.id)
        self.session.refresh(l1)
        self.assertEqual(l1.activation_status, "awaiting_reply")
        self.assertIsNotNone(l1.next_action_at)

        # --- replies: one interested, one opt-out (classified, deterministic) ---
        inbound.handle_reply(self.session, reply_token=self._thread(l1).reply_token,
                             text="Yes, I'd like to claim my profile", esp_message_id="rep-1",
                             occurred_at=BUSINESS, classifier=StubReplyClassifier(), when=BUSINESS)
        inbound.handle_reply(self.session, reply_token=self._thread(l2).reply_token,
                             text="please unsubscribe me", esp_message_id="rep-2",
                             occurred_at=BUSINESS, classifier=StubReplyClassifier(), when=BUSINESS)
        self.session.refresh(l1)
        self.session.refresh(l2)
        self.assertEqual(l1.activation_status, "interested")
        self.assertEqual(l2.activation_status, "do_not_contact")
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Suppression).where(Suppression.npi == l2.npi)).scalar(), 1)

        # --- tick 2 with a claim source: the interested lead claims -> activated ---
        def fetch(url):
            return "claimed" if url == CLAIM1 else "pending"
        r2 = scheduler.tick(self.session, copy_provider=StubCopyProvider(), email_provider=cap,
                            settings=SETTINGS, when=BUSINESS, claim_fetch=fetch)
        self.assertEqual(r2.activated, 1)
        self.session.refresh(l1)
        self.assertEqual(l1.activation_status, "physician_activated")
        # the opted-out lead never sends again
        self.session.refresh(l2)
        self.assertEqual(l2.activation_status, "do_not_contact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
