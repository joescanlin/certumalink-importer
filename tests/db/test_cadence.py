"""Cadence engine tests (Phase 2 task P2.4). Skips without DB.

Covers a due follow-up sending + rescheduling + bumping cadence_step, not-due skip, max-step
exhaustion, suppression skip, the interested-lead nudge, and a quiet-hours HOLD not consuming a step.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import cadence
    from certuma.config import Settings
    from certuma.copywriter import StubCopyProvider
    from certuma.db.models import Campaign, Contact, Lead, Mailbox, Message, Prospect, Suppression, Template
    from certuma.email.provider import SendResult
    from certuma_core.cadence import MAX_STEP

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)   # 10:00 CDT -> business hours
QUIET = datetime(2026, 6, 23, 6, tzinfo=timezone.utc)       # 01:00 CDT -> quiet hours
CLAIM = "https://www.certumalink.com/claim/abc"
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    sender_from_title="Provider Onboarding", postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_SA else None


class CaptureEmailProvider:
    name = "capture"

    def __init__(self):
        self.count = 0

    def send(self, email):
        self.count += 1
        return SendResult(provider_message_id=f"esp-cad-{self.count}", accepted=True)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class CadenceTests(unittest.TestCase):
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

    def _seed(self, npi, *, status="awaiting_reply", step=0, due_at, with_contact=True):
        self.session.add(Prospect(npi=npi, last_name="Reed", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        self.session.execute(update(Template).where(Template.campaign.is_(None), Template.version == 1)
                             .values(is_approved=True))
        self.session.execute(update(Mailbox).values(is_active=False))  # deterministic mailbox pick
        self.session.add(Mailbox(address=f"cad-{npi}@getcertuma.com", domain="getcertuma.com", is_active=True))
        if with_contact:
            self.session.add(Contact(npi=npi, email=f"dr.{npi}@example.com", email_status="valid"))
        lead = Lead(npi=npi, campaign="dermatology", activation_status=status, cadence_step=step,
                    next_action_at=due_at, claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        return lead

    def _run(self, when=BUSINESS):
        return cadence.run_cadence(self.session, copy_provider=StubCopyProvider(),
                                   email_provider=CaptureEmailProvider(), settings=SETTINGS, when=when)

    def test_due_followup_sends_and_reschedules(self):
        lead = self._seed("2100000001", due_at=BUSINESS - timedelta(days=1))
        s = self._run()
        self.assertEqual(s.sent, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.cadence_step, 1)              # bumped
        self.assertEqual(lead.activation_status, "email_sent")
        self.assertGreater(lead.next_action_at, BUSINESS)   # next touch scheduled
        msg = self.session.execute(
            select(Message).where(Message.lead_id == lead.id, Message.cadence_step == 1,
                                  Message.direction == "outbound")).scalar_one()
        self.assertIsNotNone(msg.esp_message_id)

    def test_not_due_is_skipped(self):
        lead = self._seed("2100000002", due_at=BUSINESS + timedelta(days=1))  # future
        s = self._run()
        self.assertEqual((s.due, s.sent), (0, 0))
        self.session.refresh(lead)
        self.assertEqual(lead.cadence_step, 0)

    def test_max_step_exhausts(self):
        lead = self._seed("2100000003", step=MAX_STEP, due_at=BUSINESS - timedelta(days=1))
        s = self._run()
        self.assertEqual(s.exhausted, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "exhausted")
        self.assertIsNone(lead.next_action_at)

    def test_suppressed_is_skipped(self):
        lead = self._seed("2100000004", due_at=BUSINESS - timedelta(days=1))
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        s = self._run()
        self.assertEqual((s.sent, s.skipped), (0, 1))
        self.session.refresh(lead)
        self.assertEqual(lead.cadence_step, 0)
        self.assertIsNone(lead.next_action_at)

    def test_interested_lead_is_nudged(self):
        lead = self._seed("2100000005", status="interested", due_at=BUSINESS - timedelta(days=1))
        s = self._run()
        self.assertEqual(s.sent, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "email_sent")  # nudged toward the claim link
        self.assertEqual(lead.cadence_step, 1)

    def test_quiet_hours_holds_without_consuming_step(self):
        lead = self._seed("2100000006", due_at=QUIET - timedelta(days=1))
        s = self._run(when=QUIET)
        self.assertEqual((s.sent, s.held), (0, 1))
        self.session.refresh(lead)
        self.assertEqual(lead.cadence_step, 0)              # NOT bumped on a HOLD
        self.assertEqual(lead.activation_status, "awaiting_reply")
        self.assertGreater(lead.next_action_at, QUIET)      # retried soon


if __name__ == "__main__":
    unittest.main(verbosity=2)
