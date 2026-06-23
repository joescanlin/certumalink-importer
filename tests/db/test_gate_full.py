"""Full-Gate tests (Phase 1 task P1.3). The new checks layered on the Phase 0 stub.

Skips without DB. Rolled-back session per test (the Gate is read-only, but seeding mutates).
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
    from certuma.config import Settings
    from certuma import gate
    from certuma.db.models import (
        AuditLog, Campaign, CircuitBreakerState, Lead, Mailbox, Message, Prospect, Template,
    )

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 14, tzinfo=timezone.utc)  # Tue 09:00 CDT
QUIET = datetime(2026, 6, 23, 2, tzinfo=timezone.utc)       # Mon 21:00 CDT
# guarded so the module imports under system python (no SQLAlchemy); the class is skipped there
FULL_SETTINGS = (
    Settings(postal_address="Certuma, 1 Main St, Austin TX", sender_from_email="jordan@getcertuma.com")
    if HAVE_SA else None
)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class FullGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "circuit_breaker_state" not in inspect(cls.engine).get_table_names():
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

    def _prospect(self, npi="1700000001", state="TX"):
        self.session.add(Prospect(npi=npi, practice_state=state, display_name="Dr T"))
        self.session.flush()

    def _approve_default_template(self):
        self.session.execute(
            update(Template).where(Template.campaign.is_(None), Template.version == 1).values(is_approved=True)
        )
        self.session.flush()

    # ---- preview unchanged ----
    def test_preview_without_kwargs_allows(self):
        self._prospect()
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology")
        self.assertEqual(d.decision, gate.ALLOW)

    # ---- circuit breaker ----
    def test_tripped_breaker_holds(self):
        self.session.add(CircuitBreakerState(scope="global", breaker="complaint", is_tripped=True))
        self.session.flush()
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology")
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "circuit_breaker_complaint"))

    # ---- CAN-SPAM ----
    def test_can_spam_incomplete_when_no_postal(self):
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology",
                          settings=Settings(postal_address="", sender_from_email="x@y.com"))
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "can_spam_incomplete"))

    def test_can_spam_incomplete_when_no_approved_template(self):
        # full settings but no approved template -> still HOLD
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology",
                          settings=FULL_SETTINGS)
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "can_spam_incomplete"))

    def test_can_spam_ok_with_full_config(self):
        self._prospect()
        self._approve_default_template()
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology",
                          settings=FULL_SETTINGS)
        self.assertEqual(d.decision, gate.ALLOW)  # no quiet/warmup kwargs, breaker clear

    # ---- quiet hours ----
    def test_quiet_hours_holds(self):
        self._prospect(state="TX")
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology", when=QUIET)
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "quiet_hours"))

    def test_business_hours_passes_quiet_check(self):
        self._prospect(state="TX")
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology", when=BUSINESS)
        self.assertEqual(d.decision, gate.ALLOW)

    # ---- warmup cap ----
    def test_warmup_cap_holds(self):
        self._prospect()
        self.session.add(Campaign(name="t", label="T"))  # for the lead/message FK
        mb = Mailbox(address="rep1@getcertuma.com", daily_cap=1)
        self.session.add(mb)
        self.session.flush()
        lead = Lead(npi="1700000001", campaign="t", activation_status="email_sent")
        self.session.add(lead)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, mailbox_id=mb.id, npi="1700000001", campaign="t",
                                 cadence_step=1, direction="outbound", sent_at=BUSINESS))
        self.session.flush()
        d = gate.evaluate(self.session, npi="1700000001", email=None, campaign="t",
                          when=BUSINESS, mailbox=mb)
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "warmup_cap_exceeded"))

    # ---- read-only invariant with all kwargs ----
    def test_gate_writes_nothing(self):
        self._prospect()
        before = self.session.execute(select(func.count()).select_from(AuditLog)).scalar()
        gate.evaluate(self.session, npi="1700000001", email=None, campaign="dermatology",
                      when=BUSINESS, settings=FULL_SETTINGS)
        after = self.session.execute(select(func.count()).select_from(AuditLog)).scalar()
        self.assertEqual(before, after)
        # campaign.is_paused must be untouched
        self.assertFalse(self.session.execute(
            select(Campaign.is_paused).where(Campaign.name == "dermatology")
        ).scalar())


if __name__ == "__main__":
    unittest.main(verbosity=2)
