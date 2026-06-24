"""Learning loop DB tests (Phase 3 task P3.7): variant assignment + performance + winner. Skips without DB."""
from __future__ import annotations

import os
import sys
import unittest
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
    from certuma import learning
    from certuma.config import Settings
    from certuma.copywriter import StubCopyProvider, draft_email
    from certuma.db.models import Campaign, Lead, Message, Prospect, Template

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BODY = ("Hi Dr. {last_name}, your {pitch_angle} in {city}. Review: {claim_url}. "
        "Unsubscribe: {unsubscribe_url}. {postal_address}")
SETTINGS = Settings(postal_address="Certuma, 1 Main St, Austin TX 78701",
                    cold_domain="getcertuma.com") if HAVE_SA else None


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class LearningDbTests(unittest.TestCase):
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

    def _two_variants(self):
        self.session.add(Template(campaign="dermatology", version=1, subject="A", body=BODY,
                                  variant_label="A", is_approved=True))
        self.session.add(Template(campaign="dermatology", version=2, subject="B", body=BODY,
                                  variant_label="B", is_approved=True))
        self.session.flush()

    def _lead(self, npi):
        self.session.add(Prospect(npi=npi, last_name="Var", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="sendable",
                    claim_url="https://www.certumalink.com/claim/x")
        self.session.add(lead)
        self.session.flush()
        return lead

    def test_copywriter_assigns_a_stable_variant(self):
        self._two_variants()
        lead = self._lead("2800000001")
        r1 = draft_email(self.session, lead, provider=StubCopyProvider(), settings=SETTINGS)
        self.assertTrue(r1.ok, r1.violations)
        self.assertIn(r1.rendered.variant_id, ("A", "B"))
        r2 = draft_email(self.session, lead, provider=StubCopyProvider(), settings=SETTINGS)
        self.assertEqual(r1.rendered.variant_id, r2.rendered.variant_id)  # stable per npi

    def test_variant_performance_and_winner(self):
        # variant A: 2 sent, 1 activated; variant B: 2 sent, 0 activated
        plan = [("2800000010", "A", "physician_activated"), ("2800000011", "A", "awaiting_reply"),
                ("2800000012", "B", "awaiting_reply"), ("2800000013", "B", "email_sent")]
        for npi, variant, status in plan:
            self.session.add(Prospect(npi=npi, last_name="Perf"))
            self.session.flush()
            lead = Lead(npi=npi, campaign="dermatology", activation_status=status)
            self.session.add(lead)
            self.session.flush()
            self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                     direction="outbound", variant_id=variant, esp_message_id=f"o-{npi}"))
            if npi == "2800000011":  # one A lead replied
                self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                         direction="inbound", body_rendered="hi", esp_message_id=f"i-{npi}"))
        self.session.flush()

        perf = {r["variant"]: r for r in learning.variant_performance(self.session, campaign="dermatology")}
        self.assertEqual(perf["A"]["sent"], 2)
        self.assertEqual(perf["A"]["activated"], 1)
        self.assertEqual(perf["A"]["replied"], 1)
        self.assertEqual(perf["B"]["activated"], 0)
        self.assertEqual(learning.winning_variant(self.session, campaign="dermatology", min_sample=2), "A")


if __name__ == "__main__":
    unittest.main(verbosity=2)
