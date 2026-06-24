"""Clinician knowledge-graph / signals tests (Phase 3 task P3.3). Skips without DB."""
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
    from certuma import signals
    from certuma.config import Settings
    from certuma.db.models import ClinicianSignal, Lead, Prospect

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class SignalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "clinician_signal" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("migration 0007 not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _seed(self, npi, *, with_lead=True, specialty="Dermatology", state="TX"):
        self.session.add(Prospect(npi=npi, last_name="Sig", primary_specialty=specialty,
                                  practice_state=state, practice_city="Austin"))
        self.session.flush()
        if with_lead:
            self.session.add(Lead(npi=npi, campaign="dermatology", activation_status="sendable"))
            self.session.flush()

    def _types(self, npi):
        return {r[0] for r in self.session.execute(
            select(ClinicianSignal.signal_type).where(ClinicianSignal.npi == npi)).all()}

    def test_collects_public_and_vendor_signals(self):
        self._seed("2600000001")
        summary = signals.run_signal_collection(self.session, when=WHEN)
        self.assertGreaterEqual(summary.clinicians, 1)
        types = self._types("2600000001")
        for t in (signals.SPECIALTY_BOARD, signals.STATE_LICENSE, signals.GROUP_SIZE,
                  signals.MESSAGE_BURDEN, signals.PUBLIC_ACTIVITY, signals.EHR, signals.PANEL_SIZE):
            self.assertIn(t, types)
        # the specialty-board signal carries the specialty value
        v = self.session.execute(select(ClinicianSignal.value).where(
            ClinicianSignal.npi == "2600000001",
            ClinicianSignal.signal_type == signals.SPECIALTY_BOARD)).scalar()
        self.assertEqual(v, "Dermatology")

    def test_upsert_is_idempotent(self):
        self._seed("2600000002")
        signals.run_signal_collection(self.session, when=WHEN)
        n1 = self.session.execute(select(func.count()).select_from(ClinicianSignal)
                                  .where(ClinicianSignal.npi == "2600000002")).scalar()
        signals.run_signal_collection(self.session, when=WHEN)  # second pass upserts, no dupes
        n2 = self.session.execute(select(func.count()).select_from(ClinicianSignal)
                                  .where(ClinicianSignal.npi == "2600000002")).scalar()
        self.assertEqual(n1, n2)

    def test_skips_prospects_without_a_lead(self):
        self._seed("2600000003", with_lead=False)
        signals.run_signal_collection(self.session, when=WHEN)
        self.assertEqual(self._types("2600000003"), set())

    def test_only_public_provider(self):
        self._seed("2600000004")
        signals.run_signal_collection(self.session, providers=[signals.PublicSignalProvider()], when=WHEN)
        types = self._types("2600000004")
        self.assertIn(signals.MESSAGE_BURDEN, types)
        self.assertNotIn(signals.EHR, types)  # vendor provider not used


if __name__ == "__main__":
    unittest.main(verbosity=2)
