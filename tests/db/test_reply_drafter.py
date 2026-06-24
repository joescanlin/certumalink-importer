"""Reply drafter tests (Phase 2 task P2.3). Skips without DB."""
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
    from certuma import reply_drafter
    from certuma.config import Settings
    from certuma.db.models import Approval, Campaign, Lead, Message, Prospect

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)
CLAIM = "https://www.certumalink.com/claim/abc"
SETTINGS = Settings(postal_address="Certuma, 1 Main St, Austin TX 78701",
                    cold_domain="getcertuma.com") if HAVE_SA else None


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ReplyDrafterTests(unittest.TestCase):
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

    def _seed(self, npi, *, intent="objection", text_body="how much does this cost?"):
        self.session.add(Prospect(npi=npi, last_name="Reed", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="needs_review", claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                 direction="inbound", body_rendered=text_body, esp_message_id=f"in-{npi}",
                                 reply_classification=intent))
        self.session.flush()
        return lead

    def _reply_approval(self, lead_id):
        return self.session.execute(
            select(Approval).where(Approval.lead_id == lead_id, Approval.proposed_action == "reply")
        ).scalar()

    def test_drafts_a_compliant_reply_for_an_objection(self):
        lead = self._seed("2400000001", intent="objection")
        s = reply_drafter.draft_pending_replies(self.session, settings=SETTINGS, when=WHEN)
        self.assertEqual(s.drafted, 1)
        appr = self._reply_approval(lead.id)
        self.assertIsNotNone(appr)
        self.assertEqual(appr.gate_reason_code, "objection")
        self.assertEqual(appr.state, "pending")
        for token in (CLAIM, "getcertuma.com/u/" + lead.npi, SETTINGS.postal_address):
            self.assertIn(token, appr.proposed_body)
        self.assertNotIn("{", appr.proposed_body)  # all compliance tokens rendered

    def test_is_idempotent(self):
        lead = self._seed("2400000002")
        reply_drafter.draft_pending_replies(self.session, settings=SETTINGS, when=WHEN)
        s2 = reply_drafter.draft_pending_replies(self.session, settings=SETTINGS, when=WHEN)
        self.assertEqual(s2.drafted, 0)
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Approval).where(Approval.lead_id == lead.id,
                                                             Approval.proposed_action == "reply")).scalar(), 1)

    def test_skips_non_objection_inbound(self):
        lead = self._seed("2400000003", intent="interested")
        s = reply_drafter.draft_pending_replies(self.session, settings=SETTINGS, when=WHEN)
        self.assertEqual(s.drafted, 0)
        self.assertIsNone(self._reply_approval(lead.id))

    def test_drafts_for_a_question(self):
        lead = self._seed("2400000004", intent="question", text_body="how do I claim?")
        s = reply_drafter.draft_pending_replies(self.session, settings=SETTINGS, when=WHEN)
        self.assertEqual(s.drafted, 1)
        self.assertEqual(self._reply_approval(lead.id).gate_reason_code, "question")


if __name__ == "__main__":
    unittest.main(verbosity=2)
