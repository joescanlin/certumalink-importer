"""Engagement-signal play tests (Phase 3 task P3.6). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.cadence import MAX_STEP
from certuma_core.engagement import (CHURN_RISK, COLD, OPENED_NO_REPLY, QUIET_DAYS, REPLIED,
                                     WENT_QUIET, classify, play_for)


class EngagementTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(classify(replied=True, open_count=3, days_since_engaged=1, cadence_step=0), REPLIED)
        self.assertEqual(classify(replied=False, open_count=0, days_since_engaged=None, cadence_step=0), COLD)
        self.assertEqual(classify(replied=False, open_count=2, days_since_engaged=1, cadence_step=0),
                         OPENED_NO_REPLY)
        self.assertEqual(classify(replied=False, open_count=2, days_since_engaged=QUIET_DAYS + 5,
                                  cadence_step=MAX_STEP - 1), WENT_QUIET)
        self.assertEqual(classify(replied=False, open_count=2, days_since_engaged=QUIET_DAYS + 5,
                                  cadence_step=MAX_STEP), CHURN_RISK)

    def test_only_flagged_states_have_a_play(self):
        self.assertIsNotNone(play_for(OPENED_NO_REPLY))
        self.assertIsNotNone(play_for(WENT_QUIET))
        self.assertIsNotNone(play_for(CHURN_RISK))
        self.assertIsNone(play_for(COLD))
        self.assertIsNone(play_for(REPLIED))


if __name__ == "__main__":
    unittest.main(verbosity=2)
