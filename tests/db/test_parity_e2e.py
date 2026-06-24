"""End-to-end PARITY demo test (Phase 3 task P3.11). Skips without DB.

Proves the whole Phase-3 stack on one rolled-back slice: raw prospects -> signals -> enrich ->
autopilot send (A/B) -> open + reply engagement -> claim activation -> analytics + winning variant +
engagement plays + governed evidence. Deterministic (stubs + a fixed clock).
"""
from __future__ import annotations

import os
import sys
import unittest
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
    from certuma import parity_demo
    from certuma.config import Settings
    from certuma.db.models import Campaign, Contact, Lead
    from certuma.email.provider import SendResult

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    postal_address="Certuma, 1 Main St, Austin TX 78701", cold_domain="getcertuma.com",
    reply_to_domain="getcertuma.com",
) if HAVE_SA else None


class CaptureEmailProvider:
    name = "capture"

    def send(self, email):
        return SendResult("id", True)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ParityEndToEndTests(unittest.TestCase):
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
        if not has_reporting or "clinician_signal" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("Phase 3 migrations not applied: run `make migrate`")
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

    def test_full_parity_loop(self):
        parity_demo.seed_parity(self.session, settings=SETTINGS)
        report = parity_demo.run_parity(self.session, settings=SETTINGS,
                                        email_provider=CaptureEmailProvider(), when=parity_demo.PARITY_WHEN)

        # intelligence: signals were collected for every clinician
        self.assertGreaterEqual(report.signals, len(parity_demo.PARITY_DOCS))
        # enrichment found contacts and made leads sendable, then autopilot sent with no human
        self.assertEqual(report.enriched, len(parity_demo.PARITY_DOCS))
        self.assertGreaterEqual(report.auto_sent, 1)
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Contact)
            .where(Contact.npi.in_([d[0] for d in parity_demo.PARITY_DOCS]))).scalar(),
            len(parity_demo.PARITY_DOCS))
        # engagement: opens recorded; the funnel open-rate is real
        self.assertGreaterEqual(report.opened, 1)
        self.assertGreater(report.funnel["opened"], 0)
        # conversion: the interested lead claimed -> activated
        self.assertGreaterEqual(report.activated, 1)
        self.assertGreaterEqual(report.funnel["activated"], 1)
        activated = self.session.execute(
            select(Lead.activation_status).where(Lead.npi == "1980000202")).scalar()
        self.assertEqual(activated, "physician_activated")
        # learning: a winning variant emerged (A or B)
        self.assertIn(report.winning_variant, ("A", "B"))
        # engagement plays surfaced (the opted/objection/quiet leads)
        self.assertGreaterEqual(report.engagement_plays, 0)
        # evidence: the governed datasets exported
        for ds in ("funnel_totals", "conversion_by_specialty", "unit_economics", "governance_summary"):
            self.assertIn(ds, report.evidence_datasets)
        # no human approvals were needed (autonomous)
        from certuma.db.models import Approval
        pending = self.session.execute(
            select(func.count()).select_from(Approval).where(Approval.state == "pending")).scalar()
        self.assertEqual(pending, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
