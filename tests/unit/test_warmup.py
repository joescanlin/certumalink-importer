"""Mailbox warmup-ramp tests (Phase 3 task P3.10). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.warmup import WARMUP_DAYS, WARMUP_START_CAP, warmup_cap


class WarmupTests(unittest.TestCase):
    def test_ramps_from_start_to_target(self):
        self.assertEqual(warmup_cap(50, 0), WARMUP_START_CAP)      # day 0 -> start cap
        self.assertEqual(warmup_cap(50, WARMUP_DAYS), 50)          # fully warmed -> target
        self.assertEqual(warmup_cap(50, WARMUP_DAYS + 10), 50)     # stays at target
        mid = warmup_cap(50, WARMUP_DAYS / 2)
        self.assertTrue(WARMUP_START_CAP < mid < 50)               # monotonic ramp in between

    def test_monotonic(self):
        caps = [warmup_cap(100, d) for d in range(0, WARMUP_DAYS + 1)]
        self.assertEqual(caps, sorted(caps))

    def test_small_target_is_not_inflated(self):
        # a target below the start cap is returned as-is (never ramped UP past the target)
        self.assertEqual(warmup_cap(5, 0), 5)

    def test_unknown_age_uses_target(self):
        self.assertEqual(warmup_cap(50, None), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
