"""SENDER tests (Phase 1 task P1.4): at-most-once, ordering, headers, gate, Mailpit roundtrip.

Skips without DB. The real-send test additionally needs the isolated Mailpit (11026).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import unittest
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, func, inspect, select, text, update
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.db.models import Lead, Mailbox, Message, Prospect, Suppression, Template, Thread
    from certuma.email import MailpitProvider
    from certuma.email.provider import SendResult
    from certuma.sender import RenderedEmail, send_one

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 14, tzinfo=timezone.utc)  # Tue 09:00 CDT (TX) -> not quiet
MAILPIT_SMTP = int(os.environ.get("CERTUMA_MAILPIT_SMTP_PORT", "11026"))
MAILPIT_API = os.environ.get("CERTUMA_MAILPIT_API", "http://127.0.0.1:18026")

SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_SA else None

CLAIM = "https://www.certumalink.com/claim/abc"
UNSUB = "https://getcertuma.com/u/abc"


def _rendered(subject="Your dermatology profile"):
    body = (f"<p>Hi Dr Spine, review your dermatology profile: {CLAIM}. "
            f"Unsubscribe: {UNSUB}. Certuma, 1 Main St, Austin TX 78701</p>")
    return RenderedEmail(subject=subject, body=body, plaintext=body, variant_id="v1",
                         unsubscribe_url=UNSUB, unsubscribe_mailto="mailto:unsub@getcertuma.com")


class CaptureProvider:
    name = "capture"

    def __init__(self, session=None, lead=None, fail=False):
        self.session, self.lead, self.fail = session, lead, fail
        self.outbound = None
        self.msg_count_at_send = None

    def send(self, email):
        self.outbound = email
        if self.session is not None and self.lead is not None:
            self.msg_count_at_send = self.session.execute(
                select(func.count()).select_from(Message).where(
                    Message.lead_id == self.lead.id, Message.direction == "outbound")
            ).scalar()
        if self.fail:
            raise RuntimeError("esp down")
        return SendResult("mid-capture-1", True)


def _mailpit_up():
    try:
        with socket.create_connection(("127.0.0.1", MAILPIT_SMTP), timeout=1):
            return True
    except OSError:
        return False


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class SenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "mailbox" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated to 0003: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _seed(self, status="sendable", npi="1700000007"):
        self.session.add(Prospect(npi=npi, practice_state="TX", display_name="Dr Spine"))
        self.session.execute(
            update(Template).where(Template.campaign.is_(None), Template.version == 1).values(is_approved=True))
        mb = Mailbox(address="rep1@getcertuma.com", display_name="Jordan Avery",
                     domain="getcertuma.com", daily_cap=100)
        self.session.add(mb)
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status=status, cadence_step=0)
        self.session.add(lead)
        self.session.flush()
        return lead, mb

    def test_message_written_before_provider_send(self):
        lead, mb = self._seed()
        prov = CaptureProvider(session=self.session, lead=lead)
        out = send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                       rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        self.assertTrue(out.sent)
        self.assertEqual(prov.msg_count_at_send, 1)  # idempotency Message existed BEFORE the ESP call
        self.assertEqual(lead.activation_status, "email_sent")
        self.assertEqual(lead.version, 1)

    def test_reply_to_and_unsubscribe_headers(self):
        lead, mb = self._seed()
        prov = CaptureProvider()
        send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                 rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        thread = self.session.execute(select(Thread).where(Thread.lead_id == lead.id)).scalar_one()
        self.assertEqual(prov.outbound.reply_to, f"reply+{thread.reply_token}@getcertuma.com")
        self.assertIn("List-Unsubscribe", prov.outbound.headers)
        self.assertIn(UNSUB, prov.outbound.headers["List-Unsubscribe"])

    def test_provider_failure_rolls_back_atomically(self):
        lead, mb = self._seed()
        prov = CaptureProvider(fail=True)
        with self.assertRaises(RuntimeError):
            with self.session.begin_nested():
                send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                         rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")  # not advanced
        self.assertEqual(lead.version, 0)
        self.assertEqual(
            self.session.execute(select(func.count()).select_from(Message).where(Message.lead_id == lead.id)).scalar(),
            0,  # Message rolled back with the transition (key freed)
        )

    def test_gate_hold_does_not_send(self):
        lead, mb = self._seed()
        self.session.execute(text("UPDATE kill_switch SET is_active = true WHERE id = 1"))
        self.session.flush()
        prov = CaptureProvider()
        out = send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                       rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        self.assertFalse(out.sent)
        self.assertEqual(out.decision.reason_code, "kill_switch")
        self.assertIsNone(prov.outbound)  # provider never called
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")
        self.assertEqual(lead.version, 0)

    def test_duplicate_idempotency_key_blocks_second_send(self):
        # the core at-most-once guarantee: a pre-existing outbound key makes the send raise
        # IntegrityError at the Message insert (inside transition), BEFORE provider.send.
        lead, mb = self._seed()
        self.session.add(Message(lead_id=lead.id, npi=lead.npi, campaign=lead.campaign,
                                 cadence_step=0, direction="outbound"))
        self.session.flush()
        prov = CaptureProvider()
        with self.assertRaises(IntegrityError):
            with self.session.begin_nested():
                send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                         rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        self.assertIsNone(prov.outbound)  # never reached the ESP call

    def test_failed_send_frees_key_then_retry_succeeds(self):
        # proves "key freed, safe retry": a failed attempt rolls back the Message, and a retry
        # with a working provider then succeeds (the unique key was released).
        lead, mb = self._seed()
        with self.assertRaises(RuntimeError):
            with self.session.begin_nested():
                send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                         rendered=_rendered(), provider=CaptureProvider(fail=True),
                         settings=SETTINGS, when=BUSINESS)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")
        self.assertEqual(lead.version, 0)
        out = send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                       rendered=_rendered(), provider=CaptureProvider(), settings=SETTINGS, when=BUSINESS)
        self.assertTrue(out.sent)
        self.assertEqual(lead.activation_status, "email_sent")
        self.assertEqual(lead.version, 1)
        self.assertEqual(
            self.session.execute(select(func.count()).select_from(Message)
                                 .where(Message.lead_id == lead.id, Message.direction == "outbound")).scalar(),
            1,
        )

    def test_gate_block_suppression_does_not_send(self):
        lead, mb = self._seed()
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        prov = CaptureProvider()
        out = send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                       rendered=_rendered(), provider=prov, settings=SETTINGS, when=BUSINESS)
        self.assertFalse(out.sent)
        self.assertEqual(out.decision.reason_code, "suppression")
        self.assertTrue(out.terminal)  # BLOCK is terminal: caller must stop the lead, not requeue
        self.assertIsNone(prov.outbound)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")

    def test_render_missing_postal_raises_before_any_state_change(self):
        lead, mb = self._seed()
        bad = RenderedEmail(subject="s", body=f"<p>claim {CLAIM} unsub {UNSUB}</p>",
                            plaintext=f"claim {CLAIM} unsub {UNSUB}", variant_id="v1",
                            unsubscribe_url=UNSUB, unsubscribe_mailto="mailto:u@x")  # no postal address
        prov = CaptureProvider()
        with self.assertRaises(ValueError):
            send_one(self.session, lead, mailbox=mb, to_email="dr@example.com",
                     rendered=bad, provider=prov, settings=SETTINGS, when=BUSINESS)
        self.assertIsNone(prov.outbound)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")

    @unittest.skipUnless(_mailpit_up(), "isolated Mailpit not reachable (run `make db-up`)")
    def test_happy_path_sends_real_email(self):
        lead, mb = self._seed()
        subject = f"Certuma send {uuid.uuid4().hex[:8]}"
        provider = MailpitProvider("127.0.0.1", MAILPIT_SMTP)
        out = send_one(self.session, lead, mailbox=mb, to_email="recipient@example.com",
                       rendered=_rendered(subject=subject), provider=provider, settings=SETTINGS, when=BUSINESS)
        self.assertTrue(out.sent)
        self.assertEqual(lead.activation_status, "email_sent")
        msg = self.session.execute(select(Message).where(Message.lead_id == lead.id)).scalar_one()
        self.assertEqual(msg.esp_message_id, out.esp_message_id)
        self.assertIsNotNone(msg.sent_at)
        with urllib.request.urlopen(f"{MAILPIT_API}/api/v1/search?query={urllib.parse.quote(subject)}", timeout=5) as r:
            data = json.load(r)
        self.assertIn(subject, [m.get("Subject") for m in data.get("messages", [])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
