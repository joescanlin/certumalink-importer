"""Inbound reply ingestion + classification tests (Phase 2 tasks P2.1 / P2.2). Skips without DB.

Covers threading, dedup, unmatched tokens, the -> replied transition, and each classified intent's
deterministic effect (interested, unsubscribe, not-interested, objection, question, OOO reschedule),
plus the safety that a reply never activates a lead.
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
    from sqlalchemy import create_engine, func, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import gate, inbound
    from certuma.classifier import RESCHEDULE_DAYS, StubReplyClassifier
    from certuma.config import Settings
    from certuma.db.models import Campaign, Event, Lead, Message, Prospect, Suppression, Thread

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class InboundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        cols = {c["name"] for c in inspect(cls.engine).get_columns("message")} if \
            "message" in inspect(cls.engine).get_table_names() else set()
        if "reply_classification" not in cols:
            raise unittest.SkipTest("migration 0004 not applied: run `make migrate`")
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

    def _seed(self, npi, status="awaiting_reply", token=None):
        token = token or f"tok-{npi}"
        self.session.add(Prospect(npi=npi, last_name="Reed", practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status=status,
                    claim_url="https://www.certumalink.com/claim/x")
        self.session.add(lead)
        self.session.flush()
        thread = Thread(lead_id=lead.id, reply_token=token)
        self.session.add(thread)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, thread_id=thread.id, npi=npi, campaign="dermatology",
                                 cadence_step=0, direction="outbound", subject="Your profile",
                                 esp_message_id="out-1"))
        self.session.flush()
        return lead, thread

    def _supp(self, npi):
        return self.session.execute(
            select(func.count()).select_from(Suppression).where(Suppression.npi == npi)
        ).scalar()

    # ---- ingestion ----
    def test_ingest_threads_and_moves_to_replied(self):
        lead, _ = self._seed("2000000001")
        res = inbound.ingest_reply(self.session, reply_token="tok-2000000001",
                                   text="thanks for reaching out", esp_message_id="in-1", occurred_at=WHEN)
        self.assertTrue(res.matched)
        self.assertEqual(res.transitioned_to, "replied")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "replied")
        msg = self.session.execute(
            select(Message).where(Message.lead_id == lead.id, Message.direction == "inbound")
        ).scalar_one()
        self.assertIsNotNone(msg.in_reply_to)  # linked to the outbound it answers
        # a replied Event was recorded
        self.assertIsNotNone(self.session.execute(
            select(Event).where(Event.lead_id == lead.id, Event.event_type == "replied")).scalar())

    def test_ingest_dedup(self):
        self._seed("2000000002")
        inbound.ingest_reply(self.session, reply_token="tok-2000000002", text="hi", esp_message_id="dup-1",
                             occurred_at=WHEN)
        res2 = inbound.ingest_reply(self.session, reply_token="tok-2000000002", text="hi", esp_message_id="dup-1",
                                    occurred_at=WHEN)
        self.assertTrue(res2.duplicate)

    def test_ingest_unmatched_token(self):
        res = inbound.ingest_reply(self.session, reply_token="nope", text="hi", esp_message_id="x",
                                   occurred_at=WHEN)
        self.assertFalse(res.matched)

    # ---- classification effects (via handle_reply) ----
    def _handle(self, npi, text, status="awaiting_reply"):
        lead, _ = self._seed(npi, status=status)
        res, outcome = inbound.handle_reply(
            self.session, reply_token=f"tok-{npi}", text=text, esp_message_id=f"in-{npi}",
            occurred_at=WHEN, classifier=StubReplyClassifier(), when=WHEN)
        self.session.refresh(lead)
        return lead, outcome

    def test_interested_reply_moves_to_interested_and_schedules_nudge(self):
        lead, outcome = self._handle("2000000010", "Yes, sign me up, I'd like to claim")
        self.assertEqual(outcome.intent, "interested")
        self.assertEqual(lead.activation_status, "interested")
        self.assertEqual(lead.next_action_at, WHEN)  # cadence nudge marker
        self.assertFalse(outcome.escalated)

    def test_unsubscribe_reply_suppresses_and_stops(self):
        lead, outcome = self._handle("2000000011", "please unsubscribe me")
        self.assertEqual(lead.activation_status, "do_not_contact")
        self.assertEqual(self._supp(lead.npi), 1)
        d = gate.evaluate(self.session, npi=lead.npi, email=None, campaign="dermatology")
        self.assertEqual((d.decision, d.reason_code), (gate.BLOCK, "suppression"))

    def test_not_interested_reply_suppresses(self):
        lead, outcome = self._handle("2000000012", "not interested, thanks")
        self.assertEqual(outcome.intent, "not_interested")
        self.assertEqual(lead.activation_status, "do_not_contact")
        self.assertEqual(self._supp(lead.npi), 1)

    def test_objection_reply_escalates(self):
        lead, outcome = self._handle("2000000013", "what does this cost? is this legit?")
        self.assertEqual(outcome.intent, "objection")
        self.assertTrue(outcome.escalated)
        self.assertEqual(lead.activation_status, "needs_review")

    def test_question_reply_escalates(self):
        lead, outcome = self._handle("2000000014", "Can you tell me more?")
        self.assertEqual(outcome.intent, "question")
        self.assertEqual(lead.activation_status, "needs_review")

    def test_out_of_office_reschedules(self):
        lead, outcome = self._handle("2000000015", "I am out of office until next week")
        self.assertEqual(outcome.intent, "out_of_office")
        self.assertEqual(lead.activation_status, "awaiting_reply")  # not a real reply
        self.assertEqual(lead.next_action_at, WHEN + timedelta(days=RESCHEDULE_DAYS))

    def test_reply_never_activates(self):
        # even an emphatic yes only reaches 'interested'; activation stays claim-click only
        lead, outcome = self._handle("2000000016", "yes absolutely set me up right now")
        self.assertNotEqual(lead.activation_status, "physician_activated")
        self.assertIn(lead.activation_status, ("interested", "needs_review"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
