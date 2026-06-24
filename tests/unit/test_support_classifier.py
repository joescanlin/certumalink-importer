"""StubSupportClassifier keyword-rule tests (Phase 4 / support). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma.support import EFFECTS, SUPPORT_INTENTS, StubSupportClassifier


class StubSupportClassifierTests(unittest.TestCase):
    def setUp(self):
        self.c = StubSupportClassifier()

    def test_intents_are_in_the_closed_set(self):
        for text in ("anything", "please cancel", "", "how do i do this?"):
            self.assertIn(self.c.classify(text=text).intent, SUPPORT_INTENTS)

    def test_rule_mapping(self):
        cases = {
            "We want to add more seats for our whole practice": "expansion_interest",
            "I love it, this is amazing and so helpful": "satisfaction",
            "I am frustrated and want to cancel for a refund": "complaint",
            "There is a bug, the page shows a 404 error": "bug_report",
            "Would be great if you could integrate with our EHR": "feature_request",
            "I have a question about my invoice charge": "billing",
            "I need help to finish setup and claim my profile": "onboarding_help",
            "How do I edit my listing?": "how_to",
            "asdf random words with no signal": "other",
        }
        for text, expected in cases.items():
            self.assertEqual(self.c.classify(text=text).intent, expected, text)

    def test_complaint_beats_expansion_when_both_present(self):
        # a churn signal is safer to surface than an upsell signal
        r = self.c.classify(text="I want to add more seats but honestly I am frustrated and may cancel")
        self.assertEqual(r.intent, "complaint")

    def test_benign_text_does_not_false_positive_into_complaint(self):
        # 'refunded' must not trip the complaint 'refund' substring (it is a billing question),
        # and 'switch to' in a how-to must not read as a churn complaint
        self.assertEqual(self.c.classify(text="Can my charge be refunded on the invoice?").intent, "billing")
        self.assertEqual(self.c.classify(text="How do I switch to dark mode?").intent, "how_to")

    def test_every_intent_has_an_effect(self):
        for intent in SUPPORT_INTENTS:
            self.assertIn(intent, EFFECTS)
            status, _signal, _answer = EFFECTS[intent]
            self.assertIn(status, ("answered", "escalated", "resolved"))

    def test_confidence_in_range(self):
        r = self.c.classify(text="I love it")
        self.assertTrue(0.0 <= r.confidence <= 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
