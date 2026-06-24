"""Trigger-signal fit scoring + recommended-action tests (Phase 3 task P3.4). Pure, no DB."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.intelligence import (SignalView, fit_score, fit_tier, recommend_action,
                                        support_action)


def _strong():
    return {
        "group_size": SignalView(numeric=20),
        "message_burden": SignalView(numeric=100, confidence=1.0),
        "public_activity": SignalView(value="active", confidence=1.0),
        "panel_size": SignalView(numeric=5000, confidence=1.0),
    }


class IntelligenceTests(unittest.TestCase):
    def test_fit_score_bounds_and_ranking(self):
        strong = fit_score(_strong())
        weak = fit_score({"group_size": SignalView(numeric=1),
                          "public_activity": SignalView(value="low", confidence=0.6)})
        self.assertGreater(strong, weak)
        self.assertLessEqual(strong, 100)
        self.assertGreaterEqual(weak, 0)
        self.assertEqual(fit_score({}), 0)  # no signals -> 0

    def test_recency_decay_lowers_score(self):
        fresh = fit_score(_strong())
        stale = dict(_strong())
        stale["message_burden"] = SignalView(numeric=100, confidence=1.0, age_days=400)
        self.assertLess(fit_score(stale), fresh)

    def test_fit_tier_thresholds(self):
        self.assertEqual(fit_tier(75), "high")
        self.assertEqual(fit_tier(60), "high")
        self.assertEqual(fit_tier(40), "medium")
        self.assertEqual(fit_tier(10), "low")

    def test_support_signals_shift_fit_score(self):
        base = fit_score({"group_size": SignalView(numeric=8, confidence=1.0)})
        # an expansion question lifts fit; a support-flagged churn risk pulls it down
        up = fit_score({"group_size": SignalView(numeric=8, confidence=1.0),
                        "expansion_intent": SignalView(value="expansion_interest", confidence=0.8)})
        down = fit_score({"group_size": SignalView(numeric=8, confidence=1.0),
                          "churn_risk_support": SignalView(value="complaint", confidence=0.8)})
        self.assertGreater(up, base)
        self.assertLess(down, base)
        self.assertGreaterEqual(down, 0)  # never negative

    def test_advocate_lifts_fit_less_than_expansion(self):
        adv = fit_score({"advocate": SignalView(value="satisfaction", confidence=0.8)})
        exp = fit_score({"expansion_intent": SignalView(value="expansion_interest", confidence=0.8)})
        self.assertGreater(exp, adv)
        self.assertGreater(adv, 0)

    def test_support_action_priority(self):
        self.assertIsNone(support_action({}))
        self.assertEqual(support_action({"expansion_intent": SignalView()})[0], "Upsell")
        self.assertEqual(support_action({"advocate": SignalView()})[0], "Ask for referral")
        self.assertEqual(support_action({"churn_risk_support": SignalView()})[0], "Retention outreach")
        # churn outranks upsell outranks referral - handle the unhappy customer first - and that
        # ordering is carried by the urgency rank (churn 2, upsell/referral 1), not by fit
        both = support_action({"expansion_intent": SignalView(), "churn_risk_support": SignalView(),
                               "advocate": SignalView()})
        self.assertEqual((both[0], both[2]), ("Retention outreach", 2))
        self.assertEqual(support_action({"expansion_intent": SignalView()})[2], 1)
        self.assertEqual(support_action({"advocate": SignalView()})[2], 1)

    def test_stale_support_signal_no_longer_drives_an_action(self):
        from certuma_core.intelligence import SUPPORT_ACTION_MAX_AGE_DAYS
        fresh = SignalView(confidence=0.8, age_days=5)
        stale = SignalView(confidence=0.8, age_days=SUPPORT_ACTION_MAX_AGE_DAYS + 1)
        self.assertIsNotNone(support_action({"expansion_intent": fresh}))
        self.assertIsNone(support_action({"expansion_intent": stale}))
        self.assertIsNone(support_action({"churn_risk_support": SignalView(confidence=0.0)}))  # no confidence

    def test_recommend_action_mapping(self):
        self.assertEqual(recommend_action("not_contacted", has_contact=False, due_now=False)[0], "Enrich")
        self.assertEqual(recommend_action("sendable", has_contact=True, due_now=False)[0], "Send first touch")
        self.assertEqual(recommend_action("awaiting_reply", has_contact=True, due_now=True)[0], "Send follow-up")
        self.assertEqual(recommend_action("awaiting_reply", has_contact=True, due_now=False)[0], "Wait")
        self.assertEqual(recommend_action("needs_review", has_contact=True, due_now=False)[0], "Review")
        self.assertEqual(recommend_action("physician_activated", has_contact=True, due_now=False)[0], "Done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
