"""Active-campaign seed smoke test. Skips without DB. Runs a small n in a rolled-back transaction."""
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
    from certuma import active_seed
    from certuma.config import Settings
    from certuma.db.models import Campaign, Lead

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ActiveSeedTests(unittest.TestCase):
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

    def test_seed_produces_a_spread_of_lifecycle_states(self):
        counts = active_seed.seed_active(self.session, n=120)  # n>=100 spans every lifecycle phase
        self.assertEqual(counts["total"], 120)
        # the seed spans many states + activations + escalations
        self.assertGreaterEqual(counts.get("physician_activated", 0), 1)
        self.assertGreaterEqual(counts.get("needs_review", 0), 1)
        lifecycle = {k for k in counts if k not in ("total", "pending_send_approvals")}
        self.assertGreaterEqual(len(lifecycle), 6)  # a broad spread of states
        seeded = self.session.execute(
            select(func.count()).select_from(Lead).where(Lead.npi.like("30%"))).scalar()
        self.assertEqual(seeded, 120)
        # the reporting schema was rebuilt and reflects activations
        activated = self.session.execute(text(
            "SELECT count(*) FROM reporting.fact_lead_funnel WHERE activated AND npi LIKE '30%'")).scalar()
        self.assertGreaterEqual(activated, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
