"""Learning-loop tests (Phase 3 task P3.7). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.learning import assign_variant, pick_winner


class LearningTests(unittest.TestCase):
    def test_assignment_is_stable_per_key(self):
        self.assertEqual(assign_variant(["A", "B", "C"], "1700000001"),
                         assign_variant(["A", "B", "C"], "1700000001"))
        with self.assertRaises(ValueError):
            assign_variant([], "x")

    def test_assignment_spreads_across_variants(self):
        seen = {assign_variant(["A", "B"], str(n)) for n in range(50)}
        self.assertEqual(seen, {"A", "B"})  # both variants get traffic

    def test_pick_winner_respects_sample_and_rate(self):
        stats = [
            {"variant": "A", "sent": 30, "activated": 6, "replied": 10},   # 20% activation
            {"variant": "B", "sent": 30, "activated": 3, "replied": 8},    # 10%
            {"variant": "C", "sent": 5, "activated": 5, "replied": 5},     # 100% but tiny sample
        ]
        self.assertEqual(pick_winner(stats, min_sample=20)["variant"], "A")
        self.assertIsNone(pick_winner(stats, min_sample=100))  # nobody qualifies


if __name__ == "__main__":
    unittest.main(verbosity=2)
