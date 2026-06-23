"""End-to-end spine test (Phase 0 task B16).

Exercises the whole Phase 0 spine in one rolled-back transaction: seed importer -> certuma_core
scoring -> workflow_score row -> publish payload (dry-run) -> Gate -> ledger-writer transitions
through the graph -> suppression flips the Gate to BLOCK. Asserts observability counters advance.
This is the "end-to-end dry run on a clean DB" milestone: nothing sends, nothing is committed.
"""
from __future__ import annotations

import os
import sys
import tempfile
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
    from certuma_core.models import DoctorRecord, PracticeGroup
    from certuma_core import scoring
    from certuma.config import Settings
    from certuma import gate, ledger_writer, publish, seed_importer
    from certuma.db.models import AuditLog, Campaign, Lead, Suppression, WorkflowScore
    from certuma.observability import METRICS

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class EndToEndTests(unittest.TestCase):
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
            if s.get(Campaign, "legacy") is None:
                raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)
        METRICS.reset()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_full_spine_dry_run(self):
        npi = "1700000007"
        # 1. seed importer creates the prospect stub + lead on the legacy campaign
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write("npi,activation_status,profile_url,display_name,specialty,practice_zip,last_seen_at\n"
                     f"{npi},not_contacted,,Dr Spine,Dermatology,78701,2026-06-10T00:00:00+00:00\n")
            path = fh.name
        try:
            recon = seed_importer.seed(self.session, path, dry_run=False)
            self.session.flush()
        finally:
            os.unlink(path)
        self.assertEqual(recon.leads_to_insert, 1)
        self.assertGreaterEqual(METRICS.total("seed_run"), 1)
        lead = self.session.execute(
            select(Lead).where(Lead.npi == npi, Lead.campaign == "legacy")
        ).scalar_one()
        self.assertEqual(lead.activation_status, "not_contacted")

        # 2. certuma_core scoring -> persist a workflow_score (pure lib + DB integrate)
        rec = DoctorRecord(
            npi=npi, first_name="Spine", middle_name="", last_name="Doctor", credential="MD",
            display_name="Dr Spine", primary_taxonomy_code="207N00000X", primary_specialty="Dermatology",
            practice_address_1="1 Main", practice_address_2="", practice_city="Austin",
            practice_state="TX", practice_zip="78701", practice_phone="512-555-0199", source_fetched_at="",
        )
        wf = scoring.compute_workflow_fields(
            rec, activation_status="not_contacted", campaign=None,
            practice_group=PracticeGroup(group_id="g", records=[rec]),
        )
        self.session.add(WorkflowScore(
            npi=npi, campaign="", activation_priority=wf.activation_priority,
            activation_score=wf.activation_score, priority_reason=wf.priority_reason,
            full_priority_reasons=list(wf.full_priority_reasons),
            profile_completeness_score=wf.profile_completeness_score,
            missing_profile_fields=list(wf.missing_profile_fields),
            practice_group_id=wf.practice_group_id, practice_group_size=wf.practice_group_size,
            model_version="test",
        ))
        self.session.flush()
        score = self.session.execute(select(WorkflowScore).where(WorkflowScore.npi == npi)).scalar_one()

        # 3. publish payload (dry-run) built from DB rows
        prospect = lead  # lead has npi; fetch the prospect
        from certuma.db.models import Prospect
        prospect = self.session.get(Prospect, npi)
        row = publish.profile_payload_row(prospect, score, lead)
        payload = publish.build_payload([row], campaign="", generated_at="2026-06-23T00:00:00+00:00", dry_run=True)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["profiles"][0]["npi"], npi)
        self.assertEqual(payload["profiles"][0]["activation_priority"], wf.activation_priority)

        # 4. Gate ALLOWs a clean lead
        self.assertEqual(gate.evaluate(self.session, npi=npi, email=None, campaign="legacy").decision, gate.ALLOW)
        self.assertGreaterEqual(METRICS.total("gate_decision"), 1)

        # 5. ledger-writer drives the graph forward
        steps = [("queued_today", "system"), ("enriching", "enricher"),
                 ("sendable", "enricher"), ("email_sent", "sender")]
        version = lead.version
        for i, (new_status, actor) in enumerate(steps):
            idem = (dict(lead_id=lead.id, npi=npi, campaign="legacy", cadence_step=1, direction="outbound")
                    if new_status == "email_sent" else None)
            ledger_writer.transition(self.session, lead.id, new_status, actor=actor,
                                     reason_code="spine", expected_version=version, idempotency=idem)
            version += 1
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "email_sent")
        self.assertEqual(lead.version, 4)
        audit = self.session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == str(lead.id))
        ).scalar()
        self.assertEqual(audit, 4)
        self.assertGreaterEqual(METRICS.total("ledger_transition"), 4)

        # 6. suppression flips the Gate to BLOCK
        self.session.add(Suppression(npi=npi, reason="opt_out"))
        self.session.flush()
        blocked = gate.evaluate(self.session, npi=npi, email=None, campaign="legacy")
        self.assertEqual((blocked.decision, blocked.reason_code), (gate.BLOCK, "suppression"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
