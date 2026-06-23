"""Seed-migration tests (Phase 0 tasks B11/B12, plan §8-E).

Covers normalization + legacy mapping + dedup-by-newest-instant, unknown-status abort, dry-run
reconciliation, idempotent live load, the Lead AND Prospect clobber guards, and the FK-guarded
downgrade. Skips when no DB/SQLAlchemy. Rolled-back session per test.
"""
from __future__ import annotations

import csv as _csv
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
    from certuma.db.models import Campaign, Lead, Prospect
    from certuma import seed_importer

FIXTURE = ROOT / "tests" / "golden" / "data" / "activation_status_sample.csv"
MIGRATION_0002 = ROOT / "certuma" / "db" / "alembic" / "versions" / "0002_seed_campaigns_templates.py"
DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


def _prepare_text(csv_text: str):
    return seed_importer.prepare(list(_csv.DictReader(io.StringIO(csv_text))))


def _load_migration_0002():
    spec = importlib.util.spec_from_file_location("mig0002", MIGRATION_0002)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class SeedMigrationTests(unittest.TestCase):
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

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _legacy_lead_count(self):
        return self.session.execute(
            select(func.count()).select_from(Lead).where(Lead.campaign == "legacy")
        ).scalar()

    # ---- prepare: normalize / legacy-map / dedup ----
    def test_prepare_normalizes_dedups(self):
        rows, recon = seed_importer.prepare(seed_importer.read_ledger(FIXTURE))
        self.assertEqual(recon.csv_row_count, 6)
        self.assertEqual(recon.empty_npi_skipped, 1)
        self.assertEqual(recon.npi_unique_count, 4)
        self.assertEqual(recon.legacy_rewrites, 3)  # rox_contacted, draft_profile_created, activated
        self.assertEqual(recon.unknown_statuses, [])
        by_npi = {r["npi"]: r for r in rows}
        self.assertEqual(by_npi["1000000001"]["activation_status"], "not_contacted")  # newer ts wins
        self.assertEqual(by_npi["1000000004"]["activation_status"], "physician_activated")
        self.assertEqual(by_npi["1000000002"]["activation_status"], "not_contacted")  # blank -> default

    def test_dedup_newest_instant_wins_over_row_order_and_offset(self):
        # the requirement: newest last_seen_at INSTANT wins, NOT CSV row order.
        rows, _ = _prepare_text(
            "npi,activation_status,last_seen_at\n"
            # newest instant on the EARLIER row
            "3000000001,email_sent,2026-06-10T00:00:00+00:00\n"
            "3000000001,interested,2026-06-01T00:00:00+00:00\n"
            # mixed offsets: -05:00 row is a LATER instant but sorts earlier as a raw string
            "3000000002,email_sent,2026-06-10T00:00:00-05:00\n"   # 05:00 UTC
            "3000000002,interested,2026-06-10T04:00:00+00:00\n"   # 04:00 UTC (earlier instant)
        )
        by_npi = {r["npi"]: r for r in rows}
        self.assertEqual(by_npi["3000000001"]["activation_status"], "email_sent")
        self.assertEqual(by_npi["3000000002"]["activation_status"], "email_sent")

    def test_equal_or_blank_timestamp_tiebreak_later_row_wins(self):
        rows, _ = _prepare_text(
            "npi,activation_status,last_seen_at\n"
            "4000000001,not_contacted,2026-06-01T00:00:00+00:00\n"
            "4000000001,interested,2026-06-01T00:00:00+00:00\n"   # equal instant -> later row wins
            "4000000002,interested,\n"
            "4000000002,email_sent,\n"                            # both blank -> later row wins
        )
        by_npi = {r["npi"]: r for r in rows}
        self.assertEqual(by_npi["4000000001"]["activation_status"], "interested")
        self.assertEqual(by_npi["4000000002"]["activation_status"], "email_sent")

    def test_unparseable_timestamp_aborts(self):
        with self.assertRaises(ValueError):
            _prepare_text("npi,activation_status,last_seen_at\n5000000001,interested,not-a-date\n")

    def test_missing_required_column_aborts(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write("npi,last_seen_at\n6000000001,2026-06-01T00:00:00+00:00\n")  # no activation_status
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                seed_importer.read_ledger(path)
        finally:
            os.unlink(path)

    # ---- unknown status aborts before any write ----
    def test_unknown_status_aborts(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write("npi,activation_status,last_seen_at\n2000000001,teleported,2026-06-01T00:00:00+00:00\n")
            bad = fh.name
        try:
            with self.assertRaises(ValueError):
                seed_importer.seed(self.session, bad, dry_run=True)
        finally:
            os.unlink(bad)
        self.assertEqual(self._legacy_lead_count(), 0)

    # ---- dry run writes nothing ----
    def test_dry_run_reports_without_writing(self):
        recon = seed_importer.seed(self.session, FIXTURE, dry_run=True)
        self.assertTrue(recon.dry_run)
        self.assertEqual(recon.leads_to_insert, 4)
        self.assertEqual(recon.leads_to_update, 0)
        self.assertEqual(self._legacy_lead_count(), 0)

    # ---- live load creates rows and is idempotent ----
    def test_live_load_then_idempotent(self):
        recon = seed_importer.seed(self.session, FIXTURE, dry_run=False)
        self.assertEqual(recon.leads_to_insert, 4)
        self.assertEqual(self._legacy_lead_count(), 4)
        self.assertEqual(self.session.execute(select(func.count()).select_from(Prospect)).scalar(), 4)
        lead = self.session.execute(
            select(Lead).where(Lead.npi == "1000000004", Lead.campaign == "legacy")
        ).scalar_one()
        self.assertEqual(lead.activation_status, "physician_activated")
        recon2 = seed_importer.seed(self.session, FIXTURE, dry_run=False)
        self.assertEqual(recon2.leads_to_insert, 0)
        self.assertEqual(recon2.leads_to_update, 4)
        self.assertEqual(self._legacy_lead_count(), 4)

    # ---- THE Lead clobber guard: structural, driven by the contract constant ----
    def test_lead_rerun_does_not_clobber_live_state(self):
        seed_importer.seed(self.session, FIXTURE, dry_run=False)
        self.session.flush()
        advanced = datetime(2030, 1, 1, tzinfo=timezone.utc)
        live = {"activation_status": "email_sent", "next_action_at": advanced,
                "cadence_step": 2, "claim_url": "https://claim/x", "version": 5}
        self.session.execute(
            update(Lead).where(Lead.npi == "1000000001", Lead.campaign == "legacy").values(**live)
        )
        self.session.flush()
        seed_importer.seed(self.session, FIXTURE, dry_run=False)  # nightly re-run
        self.session.flush()
        lead = self.session.execute(
            select(Lead).where(Lead.npi == "1000000001", Lead.campaign == "legacy")
        ).scalar_one()
        self.session.refresh(lead)
        # every state column in the contract is unchanged; if a new one were added to the
        # upsert set_, this loop catches it (the keys are LEAD_STATE_COLUMNS).
        for col in seed_importer.LEAD_STATE_COLUMNS:
            self.assertEqual(getattr(lead, col), live[col], f"{col} was clobbered")
        self.assertIsNotNone(lead.last_seen_at)  # the one allowed column stays populated

    # ---- Prospect clobber guard: a blank ledger value must not blank enrichment (the BLOCKER) ----
    def test_prospect_rerun_does_not_blank_enrichment(self):
        self.session.add(Prospect(
            npi="1000000009", first_name="Jane", last_name="Roe", practice_phone="512-555-0100",
            display_name="Jane Roe MD", primary_specialty="Cardiology", practice_zip="73301",
            profile_url="https://www.certumalink.com/doctors/jane-roe-1000000009"))
        self.session.flush()
        thin = ("npi,activation_status,profile_url,display_name,specialty,practice_zip,last_seen_at\n"
                "1000000009,interested,,,,,2026-06-10T00:00:00+00:00\n")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write(thin)
            path = fh.name
        try:
            seed_importer.seed(self.session, path, dry_run=False)
            self.session.flush()
        finally:
            os.unlink(path)
        p = self.session.get(Prospect, "1000000009")
        self.session.refresh(p)
        # blank ledger cells must NOT have overwritten the enriched seed columns
        self.assertEqual(p.display_name, "Jane Roe MD")
        self.assertEqual(p.primary_specialty, "Cardiology")
        self.assertEqual(p.practice_zip, "73301")
        self.assertEqual(p.profile_url, "https://www.certumalink.com/doctors/jane-roe-1000000009")
        # non-seed enrichment columns are never in the upsert and are untouched
        self.assertEqual(p.first_name, "Jane")
        self.assertEqual(p.practice_phone, "512-555-0100")

    # ---- the 0002 downgrade campaign delete is FK-guarded (does not violate after real use) ----
    def test_downgrade_campaign_delete_is_fk_guarded(self):
        seed_importer.seed(self.session, FIXTURE, dry_run=False)  # creates leads on 'legacy'
        self.session.flush()
        drop_sql = _load_migration_0002().DROP_CAMPAIGNS
        self.session.execute(text(drop_sql))  # must NOT raise foreign_key_violation
        self.session.flush()
        self.assertIsNotNone(self.session.get(Campaign, "legacy"))      # referenced -> preserved
        self.assertIsNone(self.session.get(Campaign, "dermatology"))    # unreferenced -> deleted


if __name__ == "__main__":
    unittest.main(verbosity=2)
