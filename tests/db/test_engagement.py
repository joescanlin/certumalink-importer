"""Engagement queue tests (Phase 3 task P3.6). Skips without DB."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
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
    from certuma import engagement
    from certuma.config import Settings
    from certuma.db.models import Lead, Prospect
    from certuma_core.cadence import MAX_STEP
    from certuma_core.engagement import CHURN_RISK, OPENED_NO_REPLY, QUIET_DAYS, WENT_QUIET

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
NOW = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class EngagementQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        cols = {c["name"] for c in inspect(cls.engine).get_columns("lead")} if \
            "lead" in inspect(cls.engine).get_table_names() else set()
        if "open_count" not in cols:
            raise unittest.SkipTest("migration 0008 not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _lead(self, npi, *, opens, engaged_days_ago, step, status="awaiting_reply"):
        self.session.add(Prospect(npi=npi, last_name="Eng", primary_specialty="Dermatology"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status=status, cadence_step=step,
                    open_count=opens,
                    last_open_at=(NOW - timedelta(days=engaged_days_ago)) if opens else None,
                    last_engaged_at=(NOW - timedelta(days=engaged_days_ago)) if opens else None)
        self.session.add(lead)
        self.session.flush()
        return lead

    def test_queue_flags_and_orders(self):
        self._lead("2700000001", opens=2, engaged_days_ago=1, step=0)                 # opened_no_reply
        self._lead("2700000002", opens=1, engaged_days_ago=QUIET_DAYS + 5, step=MAX_STEP - 1)  # went_quiet
        self._lead("2700000003", opens=3, engaged_days_ago=QUIET_DAYS + 5, step=MAX_STEP)       # churn_risk
        self._lead("2700000004", opens=0, engaged_days_ago=0, step=0)                 # cold -> excluded
        q = {r["npi"]: r for r in engagement.engagement_queue(self.session, now=NOW)}
        self.assertEqual(q["2700000001"]["state"], OPENED_NO_REPLY)
        self.assertEqual(q["2700000002"]["state"], WENT_QUIET)
        self.assertEqual(q["2700000003"]["state"], CHURN_RISK)
        self.assertNotIn("2700000004", q)  # never opened -> not flagged
        # ordered churn-risk first
        ordered = [r["state"] for r in engagement.engagement_queue(self.session, now=NOW)
                   if r["npi"].startswith("27000000")]
        self.assertEqual(ordered[0], CHURN_RISK)

    def test_interested_lead_is_not_a_reengage_target(self):
        self._lead("2700000005", opens=2, engaged_days_ago=1, step=0, status="interested")
        q = {r["npi"] for r in engagement.engagement_queue(self.session, now=NOW)}
        self.assertNotIn("2700000005", q)  # already replied/warm


if __name__ == "__main__":
    unittest.main(verbosity=2)
