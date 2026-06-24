"""End-to-end Assisted-loop demo test (Phase 1 tasks P1.5 + P1.12). Skips without DB.

Deterministic path: stub copy + capture email + a fixed business-hours clock proves
sendable -> propose -> approve -> send -> delivered -> poll-claim -> physician_activated in one
rolled-back transaction. The live path additionally sends a real email through the isolated
certuma-mailpit and confirms it via the Mailpit API.
"""
from __future__ import annotations

import os
import socket
import sys
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import demo
    from certuma.config import Settings
    from certuma.db.models import Campaign, Event, Lead
    from certuma.email import MailpitProvider
    from certuma.email.provider import SendResult

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)
MAILPIT_SMTP = int(os.environ.get("CERTUMA_MAILPIT_SMTP_PORT", "11026"))
MAILPIT_API = os.environ.get("CERTUMA_MAILPIT_API", "http://127.0.0.1:18026")
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    sender_from_title="Provider Onboarding", postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_SA else None


class CaptureEmailProvider:
    name = "capture"

    def __init__(self):
        self.outbound = None

    def send(self, email):
        self.outbound = email
        return SendResult(provider_message_id="esp-demo-1", accepted=True)


def _mailpit_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", MAILPIT_SMTP), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class DemoEndToEndTests(unittest.TestCase):
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

    def test_full_loop_deterministic(self):
        npi = "1999990001"
        demo.seed_demo(self.session, settings=SETTINGS, npi=npi, mailbox_address="demo-det@getcertuma.com")
        capture = CaptureEmailProvider()
        report = demo.run_demo(self.session, npi=npi, settings=SETTINGS, email_provider=capture, when=BUSINESS)

        self.assertEqual(report.proposed, 1)
        self.assertIsNotNone(report.approval_id)
        self.assertTrue(report.sent)
        self.assertEqual(report.delivered_to, "awaiting_reply")
        self.assertTrue(report.activated)
        self.assertEqual(report.final_status, "physician_activated")
        # the captured email went to the hand-seeded valid contact
        self.assertIsNotNone(capture.outbound)
        self.assertTrue(capture.outbound.to_addr.endswith("@example.com"))
        # the activation conversion was recorded
        ev = self.session.execute(
            select(Event).where(Event.npi == npi, Event.event_type == "activated")
        ).scalar()
        self.assertIsNotNone(ev)
        # lead is terminal-success
        lead = self.session.execute(select(Lead).where(Lead.npi == npi)).scalar_one()
        self.assertEqual(lead.activation_status, "physician_activated")
        self.assertIsNotNone(lead.activation_detected_at)

    @unittest.skipUnless(_mailpit_up(), "isolated Mailpit not reachable (run `make db-up`)")
    def test_live_mailpit_roundtrip(self):
        npi = "1999990002"
        demo.seed_demo(self.session, settings=SETTINGS, npi=npi, mailbox_address="demo-live@getcertuma.com")
        provider = MailpitProvider("127.0.0.1", MAILPIT_SMTP)
        report = demo.run_demo(self.session, npi=npi, settings=SETTINGS, email_provider=provider, when=BUSINESS)

        self.assertTrue(report.sent)
        self.assertEqual(report.final_status, "physician_activated")
        # the real email is in Mailpit (search by the drafted subject)
        q = urllib.parse.quote(report.subject)
        with urllib.request.urlopen(f"{MAILPIT_API}/api/v1/search?query={q}", timeout=5) as r:
            data = __import__("json").loads(r.read().decode())
        self.assertIn(report.subject, [m.get("Subject") for m in data.get("messages", [])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
