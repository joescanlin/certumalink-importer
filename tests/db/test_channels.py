"""Multi-channel tests (Phase 3 task P3.8). Skips without DB."""
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
    from sqlalchemy import create_engine, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import channels, reporting
    from certuma.reporting import queries as reporting_queries
    from certuma.config import Settings
    from certuma.db.models import Campaign, KillSwitch, Lead, Message, Prospect, Suppression

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)


class _CapProvider:
    name = "cap"

    def send(self, email):
        from certuma.email.provider import SendResult
        return SendResult("id", True)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ChannelTests(unittest.TestCase):
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
        if "channel" not in cols:
            raise unittest.SkipTest("migration 0009 not applied: run `make migrate`")
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

    def _lead(self, npi):
        self.session.add(Prospect(npi=npi, last_name="Chan"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="sendable")
        self.session.add(lead)
        self.session.flush()
        return lead

    def test_linkedin_touch_records_channel(self):
        lead = self._lead("2900000001")
        res = channels.StubLinkedInChannel().send(self.session, lead, content="Hi on LinkedIn", when=WHEN)
        self.assertTrue(res.sent)
        self.assertEqual(res.channel, "linkedin")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "email_sent")  # a touch was sent
        msg = self.session.execute(
            select(Message).where(Message.lead_id == lead.id, Message.direction == "outbound")
        ).scalar_one()
        self.assertEqual(msg.channel, "linkedin")

    def test_linkedin_honors_suppression(self):
        lead = self._lead("2900000002")
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        res = channels.StubLinkedInChannel().send(self.session, lead, content="hi", when=WHEN)
        self.assertFalse(res.sent)
        self.assertEqual(res.reason, "suppression")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")  # untouched on any channel

    def test_linkedin_honors_kill_switch_and_campaign_pause(self):
        # kill switch stops LinkedIn too (a channel-agnostic operational control)
        lead = self._lead("2900000004")
        self.session.merge(KillSwitch(id=1, is_active=True))
        self.session.flush()
        res = channels.StubLinkedInChannel().send(self.session, lead, content="hi", when=WHEN)
        self.assertFalse(res.sent)
        self.assertEqual(res.reason, "kill_switch")
        self.session.merge(KillSwitch(id=1, is_active=False))
        self.session.flush()
        # a paused campaign stops LinkedIn too
        self.session.execute(update(Campaign).where(Campaign.name == "dermatology").values(is_paused=True))
        res2 = channels.StubLinkedInChannel().send(self.session, lead, content="hi", when=WHEN)
        self.assertFalse(res2.sent)
        self.assertEqual(res2.reason, "campaign_paused")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")  # never touched on any channel

    def test_get_channel_registry(self):
        self.assertEqual(channels.get_channel("linkedin").name, "linkedin")
        self.assertEqual(channels.get_channel("email", email_provider=_CapProvider()).name, "email")
        with self.assertRaises(ValueError):
            channels.get_channel("email")  # email needs a provider
        with self.assertRaises(ValueError):
            channels.get_channel("sms")    # unknown channel

    def test_channel_flows_to_reporting(self):
        lead = self._lead("2900000003")
        channels.StubLinkedInChannel().send(self.session, lead, content="hi", when=WHEN)
        reporting.rebuild(self.session, as_of=WHEN)
        ch = self.session.execute(text(
            "SELECT channel FROM reporting.fact_touch WHERE npi = :n"), {"n": lead.npi}).scalar()
        self.assertEqual(ch, "linkedin")
        by_ch = {r["channel"] for r in reporting_queries.touches_by_channel(self.session)}
        self.assertIn("linkedin", by_ch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
