"""Reporting ELT tests (Phase 3 task P3.0). Skips without DB.

Seeds a small operational scenario, rebuilds the reporting schema in the same (rolled-back)
transaction, and asserts the facts/dims materialize correctly, including suppression governance.
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
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import reporting
    from certuma.config import Settings
    from certuma.db.models import Campaign, Event, Lead, Message, Prospect, Suppression

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
                has_reporting = c.execute(text(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name='reporting'")).first()
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if not has_reporting:
            raise unittest.SkipTest("migration 0006 not applied: run `make migrate`")
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

    def _scalar(self, sql, **params):
        return self.session.execute(text(sql), params).scalar()

    def test_rebuild_materializes_facts_and_dims(self):
        npi = "2500000001"
        self.session.add(Prospect(npi=npi, display_name="Dr Report", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="physician_activated",
                    activation_detected_at=WHEN)
        self.session.add(lead)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                 direction="outbound", variant_id="v1", sent_at=WHEN, delivered=True,
                                 esp_message_id="out-r1"))
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                 direction="inbound", body_rendered="yes", esp_message_id="in-r1"))
        self.session.add(Event(dedup_key="ev-r1", lead_id=lead.id, npi=npi, event_type="activated",
                               occurred_at=WHEN))
        # a suppressed clinician (governance)
        self.session.add(Prospect(npi="2500000099", display_name="Dr Supp"))
        self.session.add(Suppression(npi="2500000099", reason="opt_out"))
        self.session.flush()

        report = reporting.rebuild(self.session, as_of=WHEN)
        self.assertGreaterEqual(report.clinicians, 2)
        self.assertEqual(report.touches, 1)

        # dim_clinician
        self.assertEqual(self._scalar(
            "SELECT specialty FROM reporting.dim_clinician WHERE npi=:n", n=npi), "Dermatology")
        self.assertEqual(self._scalar(
            "SELECT state FROM reporting.dim_clinician WHERE npi=:n", n=npi), "TX")
        self.assertFalse(self._scalar(
            "SELECT is_suppressed FROM reporting.dim_clinician WHERE npi=:n", n=npi))
        self.assertTrue(self._scalar(
            "SELECT is_suppressed FROM reporting.dim_clinician WHERE npi='2500000099'"))

        # dim_campaign carries dermatology
        self.assertIsNotNone(self._scalar(
            "SELECT autonomy_level FROM reporting.dim_campaign WHERE campaign='dermatology'"))

        # fact_touch
        self.assertEqual(self._scalar(
            "SELECT channel FROM reporting.fact_touch WHERE npi=:n", n=npi), "email")
        self.assertTrue(self._scalar("SELECT delivered FROM reporting.fact_touch WHERE npi=:n", n=npi))
        self.assertEqual(float(self._scalar("SELECT send_cost FROM reporting.fact_touch WHERE npi=:n", n=npi)),
                         reporting.SEND_COST)
        self.assertEqual(self._scalar("SELECT variant_id FROM reporting.fact_touch WHERE npi=:n", n=npi), "v1")

        # fact_event
        self.assertEqual(self._scalar(
            "SELECT count(*) FROM reporting.fact_event WHERE npi=:n AND event_type='activated'", n=npi), 1)

        # fact_lead_funnel: full funnel for the activated lead
        row = self.session.execute(text(
            "SELECT sent, delivered, replied, activated, is_suppressed "
            "FROM reporting.fact_lead_funnel WHERE lead_id=:l"), {"l": lead.id}).one()
        self.assertEqual(tuple(row), (True, True, True, True, False))

        # meta stamped
        self.assertIsNotNone(self._scalar("SELECT rebuilt_at FROM reporting.meta WHERE id=1"))

    def test_rebuild_is_idempotent(self):
        npi = "2500000002"
        self.session.add(Prospect(npi=npi, display_name="Dr Two", primary_specialty="Cardiology"))
        self.session.flush()
        reporting.rebuild(self.session, as_of=WHEN)
        first = self._scalar("SELECT count(*) FROM reporting.dim_clinician")
        reporting.rebuild(self.session, as_of=WHEN)  # full rebuild again -> same, not doubled
        self.assertEqual(self._scalar("SELECT count(*) FROM reporting.dim_clinician"), first)

    def test_customer_intelligence_queries(self):
        from certuma.reporting import queries as rq
        npi = "2500000003"
        self.session.add(Prospect(npi=npi, display_name="Dr CI", primary_specialty="Dermatology",
                                  practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="physician_activated",
                    activation_detected_at=WHEN)
        self.session.add(lead)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                 direction="outbound", sent_at=WHEN, delivered=True, esp_message_id="o-ci"))
        self.session.flush()
        reporting.rebuild(self.session, as_of=WHEN)

        f = rq.funnel_totals(self.session)
        self.assertGreaterEqual(f["universe"], 1)
        self.assertGreaterEqual(f["sent"], 1)
        self.assertGreaterEqual(f["activated"], 1)
        self.assertIsNotNone(f["activation_rate"])

        spec = {r["label"]: r for r in rq.by_dimension(self.session, "specialty")}
        self.assertIn("Dermatology", spec)
        self.assertGreaterEqual(spec["Dermatology"]["activated"], 1)

        eco = rq.unit_economics(self.session)
        self.assertGreaterEqual(eco["activations"], 1)
        self.assertIsNotNone(eco["cost_per_activation"])

        # time to activation: sent and activated at the same instant -> 0 days
        self.assertEqual(rq.time_to_activation_days(self.session), 0.0)

        with self.assertRaises(ValueError):
            rq.by_dimension(self.session, "drop_table")  # only whitelisted dimensions


if __name__ == "__main__":
    unittest.main(verbosity=2)
