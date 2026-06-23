"""Gate stub tests (Phase 0 task B9, plan §8-G).

Skips when no DB/SQLAlchemy. Rolled-back session per test.
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
    from sqlalchemy import create_engine, func, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.db.models import AuditLog, Campaign, KillSwitch, Suppression
    from certuma import gate

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "kill_switch" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _kill(self, on=True):
        self.session.execute(update(KillSwitch).where(KillSwitch.id == 1).values(is_active=on))
        self.session.flush()

    def test_allow_when_clean(self):
        d = gate.evaluate(self.session, npi="1000000001", email=None, campaign=None)
        self.assertEqual(d.decision, gate.ALLOW)
        self.assertTrue(d.allowed)

    def test_kill_switch_holds(self):
        self._kill(True)
        d = gate.evaluate(self.session, npi="1000000001", email=None, campaign=None)
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "kill_switch"))

    def test_campaign_pause_holds(self):
        self.session.add(Campaign(name="p", label="P", is_paused=True))
        self.session.flush()
        d = gate.evaluate(self.session, npi="1000000001", email=None, campaign="p")
        self.assertEqual((d.decision, d.reason_code), (gate.HOLD, "campaign_paused"))

    def test_suppression_blocks_by_npi(self):
        self.session.add(Suppression(npi="1000000001", reason="opt_out"))
        self.session.flush()
        d = gate.evaluate(self.session, npi="1000000001", email=None, campaign=None)
        self.assertEqual((d.decision, d.reason_code), (gate.BLOCK, "suppression"))

    def test_suppression_blocks_by_email_case_insensitive(self):
        self.session.add(Suppression(email="a@x.com", reason="opt_out"))
        self.session.flush()
        # citext column => uppercase inbound still matches
        d = gate.evaluate(self.session, npi="9999999999", email="A@X.COM", campaign=None)
        self.assertEqual((d.decision, d.reason_code), (gate.BLOCK, "suppression"))

    def test_null_key_does_not_match_null_suppression_rows(self):
        # a suppression row keyed only by email must NOT block an action that has no email
        self.session.add(Suppression(email="someone@x.com", reason="opt_out"))
        self.session.flush()
        d = gate.evaluate(self.session, npi="2000000002", email=None, campaign=None)
        self.assertEqual(d.decision, gate.ALLOW)

    def test_suppression_precedence_over_kill_switch(self):
        self._kill(True)
        self.session.add(Suppression(npi="1000000001", reason="complaint"))
        self.session.flush()
        d = gate.evaluate(self.session, npi="1000000001", email=None, campaign=None)
        self.assertEqual(d.decision, gate.BLOCK)  # BLOCK floor wins over the HOLD switch

    def test_gate_is_read_only(self):
        before = self.session.execute(select(func.count()).select_from(AuditLog)).scalar()
        gate.evaluate(self.session, npi="1000000001", email=None, campaign=None)
        after = self.session.execute(select(func.count()).select_from(AuditLog)).scalar()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
