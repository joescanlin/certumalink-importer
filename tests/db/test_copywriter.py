"""COPYWRITER node tests (Phase 1 task P1.8). Stub provider, no network. Skips without DB."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.copywriter import OPUS, SONNET, StubCopyProvider, draft_email, select_model
    from certuma.copywriter.provider import CopyOutput
    from certuma_core.copy_schema import SeedFacts
    from certuma.db.models import Lead, Prospect, Template, WorkflowScore

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
CLAIM = "https://www.certumalink.com/claim/abc"
SETTINGS = Settings(
    cold_domain="getcertuma.com", postal_address="Certuma, 1 Main St, Austin TX 78701",
    sender_from_name="Jordan Avery", sender_from_title="Provider Onboarding",
) if HAVE_SA else None


class BannedClaimProvider:
    """A misbehaving provider that injects a banned claim (the linter must catch it)."""
    name = "bad"

    def draft(self, *, template_subject, template_body, facts, model="bad"):
        body = template_body.replace("{last_name}", facts.last_name) \
            .replace("{pitch_angle}", facts.pitch_angle).replace("{city}", facts.city)
        body += "\nYou are a board-certified specialist."
        return CopyOutput(subject="s", body=body, plaintext=body, variant_id="bad", merge_token_audit=())


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class CopywriterTests(unittest.TestCase):
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
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _seed(self, npi="1700000007", claim=CLAIM):
        self.session.add(Prospect(npi=npi, first_name="Jane", last_name="Smith", display_name="Jane Smith MD",
                                  credential="MD", primary_specialty="Dermatology",
                                  practice_city="Austin", practice_state="TX"))
        self.session.execute(
            update(Template).where(Template.campaign.is_(None), Template.version == 1).values(is_approved=True))
        lead = Lead(npi=npi, campaign="dermatology", activation_status="sendable", claim_url=claim)
        self.session.add(lead)
        self.session.flush()
        return lead

    # ---- pure ----
    def test_stub_fills_personalization_keeps_compliance(self):
        facts = SeedFacts(npi="1", first_name="Jane", last_name="Smith", display_name="Jane Smith MD",
                          credential="MD", specialty="Dermatology", city="Austin", state="TX",
                          pitch_angle="dermatology practice")
        out = StubCopyProvider().draft(
            template_subject="s", template_body="Hi Dr. {last_name}, your {pitch_angle} in {city}. {claim_url}",
            facts=facts)
        self.assertIn("Smith", out.body)
        self.assertIn("dermatology practice", out.body)
        self.assertIn("Austin", out.body)
        self.assertIn("{claim_url}", out.body)  # compliance token left literal

    def test_select_model_tiering(self):
        self.assertEqual(select_model("high", 5), OPUS)     # high-value -> Opus
        self.assertEqual(select_model("high", 1), SONNET)   # high but small practice -> Sonnet
        self.assertEqual(select_model("medium", 9), SONNET)

    # ---- node ----
    def test_draft_email_happy_path(self):
        lead = self._seed()
        result = draft_email(self.session, lead, provider=StubCopyProvider(), settings=SETTINGS)
        self.assertTrue(result.ok, result.violations)
        r = result.rendered
        self.assertIn(CLAIM, r.body)
        self.assertIn(CLAIM, r.plaintext)
        self.assertIn("getcertuma.com/u/1700000007", r.unsubscribe_url)
        self.assertIn(SETTINGS.postal_address, r.body)
        self.assertIn("Smith", r.body)
        self.assertIn("dermatology practice", r.body)
        self.assertNotIn("{", r.body)  # all tokens rendered

    def test_draft_email_no_approved_template(self):
        # seed prospect + lead but do NOT approve the template
        self.session.add(Prospect(npi="1700000008", last_name="Doe", practice_city="Austin", practice_state="TX"))
        self.session.flush()  # prospect must exist before the lead FK
        lead = Lead(npi="1700000008", campaign="dermatology", activation_status="sendable", claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        result = draft_email(self.session, lead, provider=StubCopyProvider(), settings=SETTINGS)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_approved_template")

    def test_draft_email_lint_failure_routes_to_needs_review(self):
        lead = self._seed()
        result = draft_email(self.session, lead, provider=BannedClaimProvider(), settings=SETTINGS)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "lint_failed")
        self.assertTrue(any("banned claim" in v for v in result.violations))

    def test_model_tier_from_workflow_score(self):
        lead = self._seed()
        self.session.add(WorkflowScore(npi=lead.npi, campaign="", activation_priority="high",
                                       activation_score=80, profile_completeness_score=100,
                                       practice_group_size=5, model_version="t"))
        self.session.flush()
        result = draft_email(self.session, lead, provider=StubCopyProvider(), settings=SETTINGS)
        self.assertTrue(result.ok)
        self.assertEqual(result.model, OPUS)  # high priority + group_size 5 -> Opus


if __name__ == "__main__":
    unittest.main(verbosity=2)
