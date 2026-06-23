"""Dashboard skeleton tests (Phase 0 tasks B14/B15).

Uses the join-an-external-transaction pattern: the app's get_db dependency is overridden with a
session bound to a connection whose outer transaction is rolled back in tearDown, so the handlers
may commit (savepoint release) without ever mutating the real DB. Skips when DB/SQLAlchemy/FastAPI
are absent. The load-bearing assertion: toggling the kill switch / pausing a campaign via the API
changes what the Gate returns.
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
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import Session
    HAVE_DEPS = True
except Exception:  # pragma: no cover
    HAVE_DEPS = False

if HAVE_DEPS:
    from certuma.config import Settings
    from certuma.api.app import create_app, get_db
    from certuma.db.models import Approval, Campaign, Lead, Prospect

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_DEPS else "")


@unittest.skipUnless(HAVE_DEPS, "SQLAlchemy/FastAPI not installed")
class DashboardTests(unittest.TestCase):
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
        if HAVE_DEPS and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.trans = self.conn.begin()
        self.session = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        self.app = create_app()
        self.app.dependency_overrides[get_db] = self._override
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.session.close()
        self.trans.rollback()
        self.conn.close()

    def _override(self):
        yield self.session

    def _seed_approval(self, npi="1000000001"):
        self.session.add(Prospect(npi=npi, display_name="Dr Test"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="legacy", activation_status="sendable")
        self.session.add(lead)
        self.session.flush()
        appr = Approval(lead_id=lead.id, proposed_action="first_touch", value_tier="high",
                        gate_reason_code="value_tier", state="pending")
        self.session.add(appr)
        self.session.flush()
        return appr.id

    # ---- read views ----
    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("leads", "prospects", "campaigns", "pending_approvals", "kill_switch_active", "status_counts"):
            self.assertIn(key, body)
        self.assertGreaterEqual(body["campaigns"], 6)  # seeded
        self.assertFalse(body["kill_switch_active"])

    def test_approvals_empty_then_seeded(self):
        self.assertEqual(self.client.get("/approvals").json(), [])
        aid = self._seed_approval()
        rows = self.client.get("/approvals?state=pending").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["display_name"], "Dr Test")
        self.assertEqual(rows[0]["gate_reason_code"], "value_tier")

    def test_decision_moves_approval(self):
        aid = self._seed_approval()
        r = self.client.post(f"/approvals/{aid}/decision", json={"decision": "approved"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "approved")
        self.assertEqual(self.client.get("/approvals?state=pending").json(), [])
        self.assertEqual(len(self.client.get("/approvals?state=approved").json()), 1)

    def test_decision_rejects_bad_value(self):
        aid = self._seed_approval()
        self.assertEqual(self.client.post(f"/approvals/{aid}/decision", json={"decision": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/approvals/999999/decision", json={"decision": "approved"}).status_code, 404)

    # ---- the load-bearing wiring: switch toggles change the Gate ----
    def test_kill_switch_toggles_gate(self):
        self.assertEqual(self.client.get("/gate/preview", params={"npi": "1000000001"}).json()["decision"], "ALLOW")
        self.assertEqual(self.client.post("/kill-switch", json={"active": True}).json()["kill_switch_active"], True)
        g = self.client.get("/gate/preview", params={"npi": "1000000001"}).json()
        self.assertEqual((g["decision"], g["reason_code"]), ("HOLD", "kill_switch"))
        self.client.post("/kill-switch", json={"active": False})
        self.assertEqual(self.client.get("/gate/preview", params={"npi": "1000000001"}).json()["decision"], "ALLOW")

    def test_campaign_pause_toggles_gate(self):
        self.assertEqual(self.client.post("/campaigns/dermatology/pause", json={"paused": True}).status_code, 200)
        g = self.client.get("/gate/preview", params={"npi": "1000000001", "campaign": "dermatology"}).json()
        self.assertEqual((g["decision"], g["reason_code"]), ("HOLD", "campaign_paused"))
        self.assertEqual(self.client.post("/campaigns/nope/pause", json={"paused": True}).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
