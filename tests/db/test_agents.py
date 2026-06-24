"""Agent registry / prompt store tests (Agent Studio, Phase 2). Skips without DB."""
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
    from certuma import agents
    from certuma.config import Settings
    from certuma.db.models import Agent

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class AgentStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "agent" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("migration 0005 not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _active_count(self, role):
        return self.session.execute(
            select(func.count()).select_from(Agent).where(Agent.role == role, Agent.is_active.is_(True))
        ).scalar()

    def test_ensure_seeded_idempotent(self):
        agents.ensure_seeded(self.session)
        agents.ensure_seeded(self.session)  # second call is a no-op
        for role in agents.ROLES:
            self.assertEqual(self._active_count(role), 1, role)
        # the active prompt falls back to / matches the in-code default
        self.assertEqual(agents.active_prompt(self.session, "copywriter"),
                         agents.DEFAULTS["copywriter"].prompt)

    def test_active_prompt_without_row_uses_default(self):
        # no rows seeded -> in-code default
        self.assertEqual(agents.active_prompt(self.session, "classifier"),
                         agents.DEFAULTS["classifier"].prompt)

    def test_create_activate_switches_active(self):
        agents.ensure_seeded(self.session)
        a = agents.create_agent(self.session, role="copywriter", name="Warm derm",
                                model="claude-sonnet-4-6", system_prompt="Be warm and concise.",
                                activate=True)
        self.assertEqual(self._active_count("copywriter"), 1)  # still exactly one active
        self.assertEqual(agents.get_active(self.session, "copywriter").id, a.id)
        self.assertEqual(agents.active_prompt(self.session, "copywriter"), "Be warm and concise.")

    def test_update_bumps_version(self):
        agents.ensure_seeded(self.session)
        a = agents.get_active(self.session, "classifier")
        v0 = a.version
        agents.update_agent(self.session, a.id, system_prompt="New labeling instructions.")
        self.session.refresh(a)
        self.assertEqual(a.version, v0 + 1)
        self.assertEqual(a.system_prompt, "New labeling instructions.")

    def test_create_validates(self):
        with self.assertRaises(ValueError):
            agents.create_agent(self.session, role="not_a_role", name="x", model="", system_prompt="y")
        with self.assertRaises(ValueError):
            agents.create_agent(self.session, role="copywriter", name="", model="", system_prompt="y")


if __name__ == "__main__":
    unittest.main(verbosity=2)
