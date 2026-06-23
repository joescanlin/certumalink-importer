"""Ledger-writer tests (Phase 0 task B8, plan §8-C / §8-D).

Skips when no DB/SQLAlchemy. Each test runs in a session that is rolled back, so the DB is
never mutated.
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
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.db.models import AuditLog, Campaign, Lead, Message, Prospect
    from certuma.ledger_writer import ConcurrencyConflict, IllegalActor, IllegalTransition, transition

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class LedgerWriterTests(unittest.TestCase):
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

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _seed_lead(self, status="not_contacted", npi="1000000001"):
        self.session.add(Campaign(name="t", label="T"))
        self.session.add(Prospect(npi=npi))
        self.session.flush()
        lead = Lead(npi=npi, campaign="t", activation_status=status)
        self.session.add(lead)
        self.session.flush()
        return lead

    def _audit_count(self, lead_id):
        return self.session.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.entity == "lead", AuditLog.entity_id == str(lead_id), AuditLog.action == "transition")
        ).scalar()

    def test_legal_transition_bumps_version_and_audits(self):
        lead = self._seed_lead("not_contacted")
        updated = transition(self.session, lead.id, "queued_today",
                             actor="system", reason_code="queue", expected_version=0)
        self.assertEqual(updated.activation_status, "queued_today")
        self.assertEqual(updated.version, 1)
        self.assertEqual(self._audit_count(lead.id), 1)

    def test_concurrency_conflict_leaves_lead_unchanged(self):
        lead = self._seed_lead("not_contacted")
        with self.assertRaises(ConcurrencyConflict):
            transition(self.session, lead.id, "queued_today",
                       actor="system", reason_code="x", expected_version=1)  # wrong version
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "not_contacted")
        self.assertEqual(lead.version, 0)
        self.assertEqual(self._audit_count(lead.id), 0)

    def test_illegal_transition_rejected(self):
        lead = self._seed_lead("not_contacted")
        with self.assertRaises(IllegalTransition):
            transition(self.session, lead.id, "physician_activated",
                       actor="poller", reason_code="x", expected_version=0)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "not_contacted")
        self.assertEqual(lead.version, 0)

    def test_activation_actor_guard(self):
        lead = self._seed_lead("interested")
        # reply_handler / a human must NOT be able to activate (protects the conversion metric)
        for bad_actor in ("reply_handler", "dashboard:jordan"):
            with self.assertRaises(IllegalActor):
                transition(self.session, lead.id, "physician_activated",
                           actor=bad_actor, reason_code="x", expected_version=0)
            self.session.refresh(lead)
            self.assertEqual(lead.activation_status, "interested")
            self.assertEqual(lead.version, 0)
        # the poller may
        updated = transition(self.session, lead.id, "physician_activated",
                             actor="poller", reason_code="claim_click", expected_version=0)
        self.assertEqual(updated.activation_status, "physician_activated")
        self.assertEqual(updated.version, 1)

    def test_idempotency_key_inserted_then_collides(self):
        lead = self._seed_lead("sendable")
        idem = dict(lead_id=lead.id, npi="1000000001", campaign="t", cadence_step=1, direction="outbound")
        updated = transition(self.session, lead.id, "email_sent",
                             actor="sender", reason_code="send", expected_version=0, idempotency=idem)
        self.assertEqual(updated.activation_status, "email_sent")
        self.assertEqual(updated.version, 1)
        msg_count = self.session.execute(
            select(func.count()).select_from(Message)
            .where(Message.lead_id == lead.id, Message.direction == "outbound")
        ).scalar()
        self.assertEqual(msg_count, 1)
        # a second identical outbound key collides on the partial unique index
        with self.assertRaises(IntegrityError):
            with self.session.begin_nested():
                self.session.add(Message(**idem))
                self.session.flush()


if __name__ == "__main__":
    unittest.main(verbosity=2)
