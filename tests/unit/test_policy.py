"""Autonomy policy tests (Phase 2 task P2.6). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma import policy


class PolicyTests(unittest.TestCase):
    def test_assisted_always_escalates(self):
        for tier in ("high", "medium", "low", None):
            self.assertEqual(policy.decide("assisted", tier), policy.ESCALATE)

    def test_supervised_escalates_only_high_value(self):
        self.assertEqual(policy.decide("supervised", "high"), policy.ESCALATE)
        self.assertEqual(policy.decide("supervised", "medium"), policy.AUTO_SEND)
        self.assertEqual(policy.decide("supervised", "low"), policy.AUTO_SEND)
        self.assertEqual(policy.decide("supervised", None), policy.AUTO_SEND)

    def test_autonomous_auto_sends_everything(self):
        for tier in ("high", "medium", "low", None):
            self.assertEqual(policy.decide("autonomous", tier), policy.AUTO_SEND)

    def test_unknown_autonomy_is_conservative(self):
        self.assertEqual(policy.decide("", "low"), policy.ESCALATE)
        self.assertEqual(policy.decide("bogus", "low"), policy.ESCALATE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
