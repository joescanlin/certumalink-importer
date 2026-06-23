"""Campaign + template seed tests (Phase 0 task B10, migration 0002).

Asserts the four presets plus the legacy and '' sentinels are seeded with exact boosts, and that
the placeholder template carries the unsubscribe and postal-address tokens the old Rox copy lacked.
Skips when no DB; read-only (no rollback needed but kept for symmetry).
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
    from sqlalchemy import create_engine, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.db.models import Campaign, Template

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
EXPECTED_BOOSTS = {"primary-care": 18, "dermatology": 22, "cardiology": 22, "urgent-care": 18, "legacy": 0, "": 0}


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class CampaignSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "campaign" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")
        cls.session = Session(cls.engine)
        if cls.session.get(Campaign, "legacy") is None:
            raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "session", None) is not None:
            cls.session.close()

    def test_campaigns_seeded_with_exact_boosts(self):
        for name, boost in EXPECTED_BOOSTS.items():
            camp = self.session.get(Campaign, name)
            self.assertIsNotNone(camp, f"campaign {name!r} not seeded")
            self.assertEqual(camp.priority_boost, boost, f"{name} boost")
            self.assertFalse(camp.is_active, f"{name} should seed inactive")

    def test_dermatology_specialty_terms(self):
        camp = self.session.get(Campaign, "dermatology")
        self.assertEqual(list(camp.specialty_terms), ["dermatology", "207n00000x"])

    def test_placeholder_template_is_compliant_and_unapproved(self):
        tpl = self.session.execute(
            select(Template).where(Template.campaign.is_(None), Template.version == 1)
        ).scalar_one()
        self.assertFalse(tpl.is_approved)  # must be human-approved before any send
        # the two elements the old Rox copy lacked
        self.assertIn("unsubscribe_url", tpl.merge_tokens)
        self.assertIn("postal_address", tpl.merge_tokens)
        self.assertIn("{unsubscribe_url}", tpl.body)
        self.assertIn("{postal_address}", tpl.body)
        self.assertIn("{claim_url}", tpl.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
