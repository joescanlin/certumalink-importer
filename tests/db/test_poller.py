"""Claim-poller tests (Phase 1 task P1.10). Skips without DB.

Covers: claimed -> physician_activated (actor='poller'), non-claimed no-op + last_polled_at,
idempotency across passes, the unwired default-fetch failing loudly, no-claim-url skip, and the
full inbound lifecycle delivered -> awaiting_reply -> poll-claim -> physician_activated.
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
    from certuma import monitor, poller
    from certuma.config import Settings
    from certuma.db.models import Campaign, Event, Lead, Message, Prospect
    from certuma.publish.claim_status import ClaimStatusUnavailable

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 23, 16, tzinfo=timezone.utc)
CLAIM = "https://www.certumalink.com/claim/abc"


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class PollerTests(unittest.TestCase):
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

    def _seed(self, npi, status="email_sent", claim_url=CLAIM, campaign="dermatology"):
        self.session.add(Prospect(npi=npi, last_name="Smith", practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign=campaign, activation_status=status, claim_url=claim_url)
        self.session.add(lead)
        self.session.flush()
        return lead

    def test_claimed_activates_as_poller(self):
        lead = self._seed("1700002001")
        summary = poller.poll_once(self.session, fetch=lambda url: "claimed", when=WHEN)
        self.assertEqual(summary.polled, 1)
        self.assertEqual(summary.activated, 1)
        self.assertIn(lead.npi, summary.activated_npis)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "physician_activated")
        self.assertIsNotNone(lead.activation_detected_at)
        self.assertEqual(lead.last_polled_at, WHEN)
        # the activation conversion was recorded as a deduped event
        ev = self.session.execute(
            select(Event).where(Event.npi == lead.npi, Event.event_type == "activated")
        ).scalar_one()
        self.assertEqual(ev.dedup_key, f"claim:{lead.npi}:{lead.campaign}")

    def test_not_claimed_is_noop_but_stamps_polled(self):
        lead = self._seed("1700002002")
        summary = poller.poll_once(self.session, fetch=lambda url: "pending", when=WHEN)
        self.assertEqual((summary.polled, summary.activated), (1, 0))
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "email_sent")
        self.assertEqual(lead.last_polled_at, WHEN)

    def test_idempotent_across_passes(self):
        lead = self._seed("1700002003")
        s1 = poller.poll_once(self.session, fetch=lambda url: "claimed", when=WHEN)
        s2 = poller.poll_once(self.session, fetch=lambda url: "claimed", when=WHEN)
        self.assertEqual(s1.activated, 1)
        # second pass: lead is terminal (physician_activated) -> not even selectable -> no re-activate
        self.assertEqual(s2.activated, 0)
        n = self.session.execute(
            select(func.count()).select_from(Event).where(Event.npi == lead.npi)
        ).scalar()
        self.assertEqual(n, 1)

    def test_activates_from_interested_state(self):
        lead = self._seed("1700002004", status="interested")
        summary = poller.poll_once(self.session, fetch=lambda url: "active", when=WHEN)
        self.assertEqual(summary.activated, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "physician_activated")

    def test_default_fetch_raises_loudly(self):
        self._seed("1700002005")
        with self.assertRaises(ClaimStatusUnavailable):
            poller.poll_once(self.session, when=WHEN)  # default fetch is unwired

    def test_lead_without_claim_url_is_skipped(self):
        self._seed("1700002006", claim_url=None)
        summary = poller.poll_once(self.session, fetch=lambda url: "claimed", when=WHEN)
        self.assertEqual(summary.polled, 0)
        self.assertEqual(summary.activated, 0)

    def test_full_inbound_lifecycle(self):
        # send already happened: seed email_sent + an outbound message
        lead = self._seed("1700002007")
        msg = Message(lead_id=lead.id, npi=lead.npi, campaign=lead.campaign, cadence_step=1,
                      direction="outbound", subject="s", esp_message_id="esp-x")
        self.session.add(msg)
        self.session.flush()
        # delivered webhook -> awaiting_reply
        monitor.ingest_event(self.session, event_type="delivered", dedup_key="life-d",
                             occurred_at=WHEN, message_id=msg.id)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "awaiting_reply")
        # claim click picked up by the poller -> physician_activated
        summary = poller.poll_once(self.session, fetch=lambda url: "claimed", when=WHEN)
        self.assertEqual(summary.activated, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "physician_activated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
