"""Monitor / event-ingestion tests (Phase 1 task P1.9). Skips without DB.

Covers: dedup idempotency, each deterministic effect (delivered, bounce, complaint, opt-out,
unsubscribe, activated-webhook), late/illegal transition no-op safety, and the bounce/complaint
circuit breaker tripping the Gate.
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
    from sqlalchemy import create_engine, func, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import breakers, gate, monitor
    from certuma.config import Settings
    from certuma.db.models import Campaign, Event, Lead, Message, Prospect, Suppression

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
OCCURRED = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "event" not in inspect(cls.engine).get_table_names():
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

    # ---- helpers ----
    def _seed_sent(self, npi, status="email_sent", campaign="dermatology", with_message=True):
        self.session.add(Prospect(npi=npi, last_name="Smith", practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign=campaign, activation_status=status,
                    claim_url="https://www.certumalink.com/claim/x")
        self.session.add(lead)
        self.session.flush()
        msg = None
        if with_message:
            msg = Message(lead_id=lead.id, npi=npi, campaign=campaign, cadence_step=1,
                          direction="outbound", subject="s", esp_message_id="esp-1")
            self.session.add(msg)
            self.session.flush()
        return lead, msg

    def _supp_count(self, **filt):
        q = select(func.count()).select_from(Suppression)
        for k, v in filt.items():
            q = q.where(getattr(Suppression, k) == v)
        return self.session.execute(q).scalar()

    # ---- dedup ----
    def test_record_event_dedup(self):
        first = monitor.record_event(self.session, event_type="opened", dedup_key="k1",
                                     occurred_at=OCCURRED, npi="1700001001")
        second = monitor.record_event(self.session, event_type="opened", dedup_key="k1",
                                      occurred_at=OCCURRED, npi="1700001001")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        n = self.session.execute(select(func.count()).select_from(Event).where(Event.dedup_key == "k1")).scalar()
        self.assertEqual(n, 1)

    # ---- delivered ----
    def test_delivered_advances_to_awaiting_reply(self):
        lead, msg = self._seed_sent("1700001002")
        r = monitor.ingest_event(self.session, event_type="delivered", dedup_key="d-2",
                                 occurred_at=OCCURRED, message_id=msg.id)
        self.assertFalse(r.duplicate)
        self.assertEqual(r.transitioned_to, "awaiting_reply")
        self.session.refresh(lead)
        self.session.refresh(msg)
        self.assertEqual(lead.activation_status, "awaiting_reply")
        self.assertTrue(msg.delivered)

    def test_delivered_duplicate_event_is_noop(self):
        lead, msg = self._seed_sent("1700001003")
        monitor.ingest_event(self.session, event_type="delivered", dedup_key="d-3",
                             occurred_at=OCCURRED, message_id=msg.id)
        r2 = monitor.ingest_event(self.session, event_type="delivered", dedup_key="d-3",
                                  occurred_at=OCCURRED, message_id=msg.id)
        self.assertTrue(r2.duplicate)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "awaiting_reply")  # not double-applied

    # ---- bounce ----
    def test_bounce_suppresses_address_and_exhausts(self):
        lead, msg = self._seed_sent("1700001004")
        r = monitor.ingest_event(self.session, event_type="bounced", dedup_key="b-4",
                                 occurred_at=OCCURRED, message_id=msg.id, npi=lead.npi,
                                 email="dead@example.com")
        self.assertEqual(r.transitioned_to, "exhausted")
        self.assertTrue(r.suppressed)
        self.session.refresh(lead)
        self.session.refresh(msg)
        self.assertEqual(lead.activation_status, "exhausted")
        self.assertTrue(lead.needs_reenrich)
        self.assertTrue(msg.bounced)
        # the dead ADDRESS is suppressed, not the npi (so re-enrichment can retry later)
        self.assertEqual(self._supp_count(email="dead@example.com", reason="hard_bounce"), 1)
        self.assertEqual(self._supp_count(npi=lead.npi), 0)

    # ---- complaint ----
    def test_complaint_suppresses_and_do_not_contact(self):
        lead, msg = self._seed_sent("1700001005", status="awaiting_reply")
        r = monitor.ingest_event(self.session, event_type="complained", dedup_key="c-5",
                                 occurred_at=OCCURRED, message_id=msg.id, npi=lead.npi,
                                 email="x@example.com")
        self.assertEqual(r.transitioned_to, "do_not_contact")
        self.session.refresh(lead)
        self.session.refresh(msg)
        self.assertEqual(lead.activation_status, "do_not_contact")
        self.assertTrue(msg.complained)
        self.assertEqual(self._supp_count(npi=lead.npi, reason="complaint"), 1)

    # ---- opt-out / unsubscribe ----
    def test_opt_out_do_not_contact_then_gate_blocks(self):
        lead, _ = self._seed_sent("1700001006", with_message=False)
        r = monitor.ingest_event(self.session, event_type="opt_out", dedup_key="o-6",
                                 occurred_at=OCCURRED, npi=lead.npi)
        self.assertEqual(r.transitioned_to, "do_not_contact")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "do_not_contact")
        decision = gate.evaluate(self.session, npi=lead.npi, email=None, campaign="dermatology")
        self.assertEqual((decision.decision, decision.reason_code), (gate.BLOCK, "suppression"))

    def test_unsubscribe_click_suppresses(self):
        lead, _ = self._seed_sent("1700001007", with_message=False)
        r = monitor.ingest_event(self.session, event_type="unsubscribe_click", dedup_key="u-7",
                                 occurred_at=OCCURRED, npi=lead.npi, email="dr@example.com")
        self.assertTrue(r.suppressed)
        self.assertEqual(r.transitioned_to, "do_not_contact")
        self.assertTrue(gate.is_suppressed(self.session, npi=lead.npi))

    # ---- late / illegal transition safety ----
    def test_late_delivered_after_opt_out_is_noop(self):
        lead, msg = self._seed_sent("1700001008")
        monitor.ingest_event(self.session, event_type="opt_out", dedup_key="o-8",
                             occurred_at=OCCURRED, npi=lead.npi)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "do_not_contact")
        # a delivered webhook arriving after the opt-out must not raise and must not move the lead
        r = monitor.ingest_event(self.session, event_type="delivered", dedup_key="d-8",
                                 occurred_at=OCCURRED, message_id=msg.id)
        self.assertIsNone(r.transitioned_to)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "do_not_contact")

    # ---- activation via the (future) platform webhook ----
    def test_activated_event_via_webhook(self):
        lead, _ = self._seed_sent("1700001009", status="awaiting_reply", with_message=False)
        r = monitor.ingest_event(self.session, event_type="activated", dedup_key="a-9",
                                 occurred_at=OCCURRED, npi=lead.npi, when=OCCURRED)
        self.assertTrue(r.activated)
        self.assertEqual(r.transitioned_to, "physician_activated")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "physician_activated")
        self.assertIsNotNone(lead.activation_detected_at)

    # ---- breaker ----
    def test_bounce_breaker_trips_gate(self):
        # below threshold sample count -> not tripped
        for _ in range(breakers.MIN_SAMPLE - 1):
            breakers.record_outcome(self.session, breaker="bounce", campaign="dermatology", is_bad=True)
        self.assertIsNone(breakers.tripped_breaker(self.session, campaign="dermatology"))
        # crossing MIN_SAMPLE with a high bad-rate trips it; the Gate then HOLDs
        breakers.record_outcome(self.session, breaker="bounce", campaign="dermatology", is_bad=True)
        self.assertEqual(breakers.tripped_breaker(self.session, campaign="dermatology"), "bounce")
        decision = gate.evaluate(
            self.session, npi="1700001010", email=None, campaign="dermatology",
            settings=Settings(),
        )
        self.assertEqual(decision.reason_code, "circuit_breaker_bounce")
        # manual reset clears it
        breakers.reset_breaker(self.session, breaker="bounce", scope=breakers.GLOBAL_SCOPE)
        breakers.reset_breaker(self.session, breaker="bounce", scope=breakers.campaign_scope("dermatology"))
        self.assertIsNone(breakers.tripped_breaker(self.session, campaign="dermatology"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
