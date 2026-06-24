"""Enrichment waterfall tests (Phase 2 task P2.5). Skips without DB."""
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
    from certuma import enrichment
    from certuma.config import Settings
    from certuma.db.models import Campaign, Contact, Lead, Prospect, Suppression

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class EnrichmentTests(unittest.TestCase):
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

    def _seed(self, npi, *, first="Jane", last="Smith", status="not_contacted"):
        self.session.add(Prospect(npi=npi, first_name=first, last_name=last,
                                  practice_city="Austin", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status=status)
        self.session.add(lead)
        self.session.flush()
        return lead

    def _run(self):
        return enrichment.run_enrichment(self.session, discovery=enrichment.StubEnrichProvider(),
                                         verify=enrichment.StubVerifyProvider(), when=WHEN)

    def test_finds_personal_contact_and_makes_sendable(self):
        lead = self._seed("2300000001")
        s = self._run()
        self.assertEqual(s.enriched, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")
        c = self.session.execute(select(Contact).where(Contact.npi == lead.npi)).scalar_one()
        self.assertEqual(c.email_status, "valid")
        self.assertFalse(c.is_role_address)        # a personal mailbox, not info@
        self.assertEqual(c.email, "jane.smith@example.com")

    def test_no_name_routes_to_needs_review(self):
        lead = self._seed("2300000002", first="", last="")
        s = self._run()
        self.assertEqual(s.no_contact, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "needs_review")
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Contact).where(Contact.npi == lead.npi)).scalar(), 0)

    def test_suppressed_is_stopped_without_spend(self):
        lead = self._seed("2300000003")
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        s = self._run()
        self.assertEqual(s.skipped, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "do_not_contact")
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Contact).where(Contact.npi == lead.npi)).scalar(), 0)

    def test_existing_contact_is_promoted(self):
        lead = self._seed("2300000004")
        self.session.add(Contact(npi=lead.npi, email="hand@seed.com", email_status="valid"))
        self.session.flush()
        s = self._run()
        self.assertEqual(s.enriched, 1)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")
        # no duplicate contact was discovered
        self.assertEqual(self.session.execute(
            select(func.count()).select_from(Contact).where(Contact.npi == lead.npi)).scalar(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
