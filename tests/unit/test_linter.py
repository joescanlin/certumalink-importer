"""Linter + copy-schema tests (Phase 1 task P1.6). Pure: no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.copy_schema import SeedFacts, allowlist_sources  # noqa: E402
from certuma_core.linter import lint  # noqa: E402

CLAIM = "https://www.certumalink.com/claim/abc123"
UNSUB = "https://getcertuma.com/u/xyz"
POSTAL = "Certuma, 1 Main St, Austin, TX 78701"

FACTS = SeedFacts(npi="1700000001", first_name="Jane", last_name="Smith", display_name="Jane Smith MD",
                  credential="MD", specialty="Dermatology", city="Austin", state="TX",
                  pitch_angle="dermatology practice")
SOURCES = allowlist_sources(FACTS, template_prose="Certuma prepared a draft profile for you.",
                            sender_identity="Jordan Avery, Provider Onboarding")


def _render(*, claim=CLAIM, extra_body="", subject="Your Austin dermatology profile"):
    body = (f"Hi Dr Smith,\n\nWe prepared a draft profile for your dermatology practice in Austin. "
            f"{extra_body}Review and claim it here: {claim}\n\n"
            f"Unsubscribe: {UNSUB}\n\n{POSTAL}")
    plaintext = body
    return dict(subject=subject, body=body, plaintext=plaintext, allowlist_sources=SOURCES,
                claim_url=CLAIM, unsubscribe_url=UNSUB, postal_address=POSTAL)


class LinterTests(unittest.TestCase):
    def test_clean_email_passes(self):
        r = lint(**_render())
        self.assertTrue(r.ok, r.violations)

    def test_banned_claim_rejected(self):
        r = lint(**_render(extra_body="You are a board-certified dermatologist. "))
        self.assertFalse(r.ok)
        self.assertTrue(any("banned claim" in v for v in r.violations))

    def test_hallucinated_proper_noun_rejected(self):
        r = lint(**_render(extra_body="We saw your work at Mayo Clinic. "))
        self.assertFalse(r.ok)
        self.assertTrue(any("Mayo Clinic" in v for v in r.violations))

    def test_real_facts_do_not_false_reject(self):
        # the physician name, city, specialty, pitch angle, and sender identity must all PASS
        r = lint(**_render(extra_body="Jane Smith, your Dermatology profile from Jordan Avery is ready. "))
        self.assertTrue(r.ok, r.violations)

    def test_altered_claim_url_rejected(self):
        params = _render()
        params["body"] = params["body"].replace(CLAIM, "https://evil.example/claim/abc123")
        params["plaintext"] = params["body"]
        r = lint(**params)
        self.assertFalse(r.ok)
        self.assertTrue(any("claim_url" in v for v in r.violations))

    def test_missing_unsubscribe_rejected(self):
        params = _render()
        params["body"] = params["body"].replace(UNSUB, "")
        r = lint(**params)
        self.assertFalse(r.ok)
        self.assertTrue(any("unsubscribe" in v for v in r.violations))

    def test_missing_postal_rejected(self):
        params = _render()
        params["body"] = params["body"].replace(POSTAL, "")
        r = lint(**params)
        self.assertFalse(r.ok)
        self.assertTrue(any("postal" in v for v in r.violations))

    def test_leftover_token_rejected(self):
        params = _render()
        params["body"] = params["body"] + "\nDear {last_name}"
        r = lint(**params)
        self.assertFalse(r.ok)
        self.assertTrue(any("unrendered" in v for v in r.violations))

    def test_claim_url_must_be_in_plaintext_too(self):
        params = _render()
        params["plaintext"] = params["plaintext"].replace(CLAIM, "see the link above")
        r = lint(**params)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
