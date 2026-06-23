"""Template-approval tests (Phase 1 task P1.7). Skips without DB/FastAPI. Rolled-back per test."""
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
    from sqlalchemy import create_engine, func, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_DEPS = True
except Exception:  # pragma: no cover
    HAVE_DEPS = False

if HAVE_DEPS:
    from certuma.config import Settings
    from certuma.api.app import create_app, get_db
    from certuma.db.models import AuditLog, Template
    from certuma.templates import TemplateNotFound, approve_template, lint_template

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_DEPS else "")


@unittest.skipUnless(HAVE_DEPS, "SQLAlchemy/FastAPI not installed")
class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "template" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")

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

    def _seed_template_id(self):
        return self.session.execute(
            select(Template.id).where(Template.campaign.is_(None), Template.version == 1)
        ).scalar_one()

    # ---- logic ----
    def test_lint_seeded_template_is_clean(self):
        tpl = self.session.get(Template, self._seed_template_id())
        self.assertEqual(lint_template(tpl), [])

    def test_lint_flags_missing_tokens_and_banned_claims(self):
        bad = Template(campaign=None, version=99, subject="You are board-certified",
                       body="No tokens here", merge_tokens=[])
        self.session.add(bad)
        self.session.flush()
        problems = lint_template(bad)
        self.assertTrue(any("unsubscribe_url" in p for p in problems))
        self.assertTrue(any("banned claim" in p for p in problems))

    def test_approve_sets_flag_and_audits(self):
        tid = self._seed_template_id()
        tpl = approve_template(self.session, tid, approved_by="jordan")
        self.assertTrue(tpl.is_approved)
        self.assertEqual(tpl.approved_by, "jordan")
        n = self.session.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.entity == "template", AuditLog.entity_id == str(tid), AuditLog.action == "approve")
        ).scalar()
        self.assertEqual(n, 1)

    def test_approve_rejects_noncompliant(self):
        bad = Template(campaign=None, version=98, subject="s", body="no tokens", merge_tokens=[])
        self.session.add(bad)
        self.session.flush()
        with self.assertRaises(ValueError):
            approve_template(self.session, bad.id, approved_by="jordan")

    def test_approve_missing_template_raises(self):
        with self.assertRaises(TemplateNotFound):
            approve_template(self.session, 999999, approved_by="jordan")

    # ---- endpoints ----
    def test_endpoints(self):
        tid = self._seed_template_id()
        self.assertEqual(self.client.get(f"/templates/{tid}/lint").json()["ok"], True)
        before = self.client.get("/templates").json()
        self.assertTrue(any(t["id"] == tid for t in before))
        r = self.client.post(f"/templates/{tid}/approve", json={"approved_by": "jordan"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_approved"])
        self.assertEqual(self.client.post("/templates/999999/approve", json={"approved_by": "x"}).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
