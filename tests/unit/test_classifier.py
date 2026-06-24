"""StubReplyClassifier keyword-rule tests (Phase 2 task P2.2). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma.classifier import INTENTS, StubReplyClassifier


class StubClassifierTests(unittest.TestCase):
    def setUp(self):
        self.c = StubReplyClassifier()

    def test_intents_are_in_the_closed_set(self):
        for text in ("anything", "please unsubscribe", ""):
            self.assertIn(self.c.classify(text=text).intent, INTENTS)

    def test_rule_mapping(self):
        cases = {
            "Please unsubscribe me from this list": "unsubscribe",
            "Not interested, thanks": "not_interested",
            "I am out of office until next week": "out_of_office",
            "You have the wrong person": "wrong_person",
            "What does this cost?": "objection",
            "Yes, sign me up": "interested",
            "Can you tell me more?": "question",
            "asdf random words": "unknown",
        }
        for text, expected in cases.items():
            self.assertEqual(self.c.classify(text=text).intent, expected, text)

    def test_conservative_ordering(self):
        # an unsubscribe wins even if the message also sounds interested
        self.assertEqual(self.c.classify(text="yes but please unsubscribe me").intent, "unsubscribe")

    def test_confidence_in_range(self):
        r = self.c.classify(text="please unsubscribe")
        self.assertTrue(0.0 <= r.confidence <= 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
