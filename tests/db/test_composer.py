"""Template composer node tests (Studio AI compose). Skips without DB.

Covers authoring + linting a draft, inserting it as a versioned A/B variant tagged with the model,
approve-on-insert (and its compliance gate), and the provider seam (stub without a key, Anthropic
with one).
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
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import composer
    from certuma.composer import ComposeRequest, StubComposeProvider
    from certuma.config import Settings
    from certuma.db.models import Template

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


class _BadProvider:
    name = "bad"

    def compose(self, req):
        # authors a body that omits the compliance tokens and makes a banned claim
        from certuma.composer import ComposeOutput
        return ComposeOutput(subject="You are board-certified!", body="No tokens here.")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class ComposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        cols = {c["name"] for c in inspect(cls.engine).get_columns("template")}
        if "message_type" not in cols:
            raise unittest.SkipTest("migration 0012 not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_compose_template_lints_clean(self):
        req = ComposeRequest(message_type="first_touch", specialty="Dermatology", brief="warm tone")
        result = composer.compose_template(self.session, request=req, provider=StubComposeProvider())
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.message_type, "first_touch")
        # composing writes nothing
        self.assertIsNone(self.session.execute(
            select(Template).where(Template.source == "ai")).scalar())

    def test_compose_flags_non_compliant_copy(self):
        req = ComposeRequest(message_type="first_touch")
        result = composer.compose_template(self.session, request=req, provider=_BadProvider())
        self.assertFalse(result.ok)
        self.assertTrue(result.problems)

    def test_insert_creates_a_versioned_ai_variant(self):
        out = StubComposeProvider().compose(ComposeRequest(message_type="follow_up_1",
                                                           specialty="Cardiology"))
        tpl = composer.insert_template(self.session, campaign="cardiology", subject=out.subject,
                                       body=out.body, message_type="follow_up_1",
                                       model="claude-opus-4-8")
        self.assertEqual(tpl.source, "ai")
        self.assertEqual(tpl.message_type, "follow_up_1")
        self.assertEqual(tpl.model, "claude-opus-4-8")
        self.assertTrue(tpl.variant_label)  # auto-assigned an A/B label
        self.assertFalse(tpl.is_approved)

    def test_insert_auto_increments_variant_labels(self):
        out = StubComposeProvider().compose(ComposeRequest(message_type="first_touch"))
        labels = []
        for _ in range(3):
            t = composer.insert_template(self.session, campaign="primary-care", subject=out.subject,
                                         body=out.body, message_type="first_touch", model="m")
            labels.append(t.variant_label)
        self.assertEqual(labels[:3], ["A", "B", "C"])
        # versions are distinct (the campaign_version unique constraint holds)
        versions = self.session.execute(
            select(Template.version).where(Template.campaign == "primary-care")).scalars().all()
        self.assertEqual(len(versions), len(set(versions)))

    def test_approve_on_insert_requires_compliance(self):
        out = StubComposeProvider().compose(ComposeRequest(message_type="first_touch"))
        tpl = composer.insert_template(self.session, campaign="dermatology", subject=out.subject,
                                       body=out.body, message_type="first_touch", model="m",
                                       approve=True)
        self.assertTrue(tpl.is_approved)
        # a non-compliant body cannot be approved on insert
        with self.assertRaises(ValueError):
            composer.insert_template(self.session, campaign="dermatology", subject="hi",
                                     body="no tokens, board-certified", message_type="first_touch",
                                     model="m", approve=True)

    def test_insert_rejects_unknown_message_type(self):
        with self.assertRaises(ValueError):
            composer.insert_template(self.session, campaign=None, subject="s",
                                     body="{claim_url}{unsubscribe_url}{postal_address}",
                                     message_type="bogus", model="m")

    def test_approve_rejects_banned_claim_in_subject_alone(self):
        # a compliant body but a banned claim in the SUBJECT must still fail the approve gate
        body = "Hi Dr. {last_name}, claim here: {claim_url}. {unsubscribe_url} {postal_address}"
        with self.assertRaises(ValueError):
            composer.insert_template(self.session, campaign="dermatology",
                                     subject="You are board-certified", body=body,
                                     message_type="first_touch", model="m", approve=True)

    def test_compliance_lists_cover_the_authoritative_template_lint(self):
        # the composer duplicates the token/banned lists to stay DB-free; guard against drift
        from certuma.composer import BANNED_CLAIMS, REQUIRED_TOKENS
        from certuma.templates import approval
        self.assertTrue(set(approval._BANNED).issubset(set(BANNED_CLAIMS)))
        self.assertEqual(set(approval._REQUIRED_TOKENS), set(REQUIRED_TOKENS))

    def test_build_provider_seam(self):
        self.assertIsInstance(composer.build_provider(Settings()), StubComposeProvider)
        live = composer.build_provider(Settings(anthropic_api_key="sk-test"))
        self.assertEqual(live.name, "anthropic")  # real adapter when a key is present


if __name__ == "__main__":
    unittest.main(verbosity=2)
