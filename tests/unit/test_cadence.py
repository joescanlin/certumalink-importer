"""Cadence scheduling-policy tests (Phase 2 task P2.4). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.cadence import INTERVAL_DAYS, MAX_STEP, is_final_step, next_action_after

WHEN = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)


class CadencePolicyTests(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(next_action_after(0, WHEN), WHEN + timedelta(days=INTERVAL_DAYS[0]))
        self.assertEqual(next_action_after(1, WHEN), WHEN + timedelta(days=INTERVAL_DAYS[1]))

    def test_final_step_returns_grace_window(self):
        # at the last step we still return a (grace) time so the lead is revisited and exhausted
        self.assertGreater(next_action_after(MAX_STEP, WHEN), WHEN)

    def test_is_final_step(self):
        self.assertFalse(is_final_step(0))
        self.assertFalse(is_final_step(MAX_STEP - 1))
        self.assertTrue(is_final_step(MAX_STEP))
        self.assertTrue(is_final_step(MAX_STEP + 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
