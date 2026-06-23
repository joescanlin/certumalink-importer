"""State-machine + scoring-config tests (Phase-0 tasks B2/B3).

Verifies the ALLOWED_TRANSITIONS graph (completeness, terminals, re-enrich, illegal backward
edges) and pins the ScoringConfig defaults so behavior parity is provable before any re-weight.

Run: PYTHONPATH=. python3 -m unittest tests.golden.test_status_machine
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core import status  # noqa: E402
from certuma_core.config import DEFAULT_SCORING_CONFIG  # noqa: E402


class StatusGraphTests(unittest.TestCase):
    def test_graph_covers_every_state(self):
        self.assertEqual(set(status.ALLOWED_TRANSITIONS.keys()), set(status.STATES))

    def test_transition_targets_are_known_states(self):
        for src, targets in status.ALLOWED_TRANSITIONS.items():
            for dst in targets:
                self.assertIn(dst, status.STATES, msg=f"{src}->{dst} targets unknown state")

    def test_every_allowed_edge_passes_assert(self):
        for src, targets in status.ALLOWED_TRANSITIONS.items():
            if src in status.TERMINAL_STATES:
                self.assertEqual(targets, frozenset(), msg=f"{src} is terminal but has out-edges")
                continue
            for dst in targets:
                status.assert_transition(src, dst)  # must not raise
                self.assertTrue(status.is_legal_transition(src, dst))

    def test_non_edges_are_rejected(self):
        for src in status.ALLOWED_TRANSITIONS:
            allowed = status.ALLOWED_TRANSITIONS[src]
            for dst in status.STATES:
                if dst in allowed:
                    continue
                self.assertFalse(status.is_legal_transition(src, dst))
                with self.assertRaises(status.IllegalTransition):
                    status.assert_transition(src, dst)

    def test_terminals_reject_all_out_transitions(self):
        for term in status.TERMINAL_STATES:
            for dst in status.STATES:
                with self.assertRaises(status.IllegalTransition):
                    status.assert_transition(term, dst)

    def test_no_backward_into_email_sent_from_activation(self):
        with self.assertRaises(status.IllegalTransition):
            status.assert_transition("physician_activated", "email_sent")

    def test_legacy_voice_edge_preserved(self):
        status.assert_transition("called_no_answer", "email_sent")  # must not raise

    def test_reenrich_edges_exist(self):
        for src in ("email_sent", "awaiting_reply", "sendable"):
            status.assert_transition(src, "enriching")

    def test_phase1_activation_edges_reachable(self):
        # a claim_url click activates independent of any reply, so the poller must reach
        # `interested` (and thus physician_activated) from a real send. (Phase 1, additive.)
        status.assert_transition("email_sent", "interested")
        status.assert_transition("awaiting_reply", "interested")
        status.assert_transition("interested", "physician_activated")

    def test_queue_excluded_states(self):
        self.assertEqual(
            status.QUEUE_EXCLUDED_STATES,
            frozenset({"physician_activated", "do_not_contact", "exhausted", "needs_review"}),
        )

    def test_activation_only_actors(self):
        self.assertEqual(status.ACTIVATION_ONLY_ACTORS, frozenset({"poller", "activation_webhook"}))


class ScoringConfigSnapshotTests(unittest.TestCase):
    def test_default_weights_pinned(self):
        c = DEFAULT_SCORING_CONFIG
        self.assertEqual(
            (c.phone, c.both_names, c.specialty_and_taxonomy, c.full_address,
             c.shared_practice, c.fresh_contact, c.completeness_high_bonus, c.completeness_low_penalty),
            (25, 10, 15, 15, 5, 5, 10, 10),
        )
        self.assertEqual(
            (c.completeness_high_threshold, c.completeness_low_threshold, c.high_tier, c.medium_tier),
            (90, 70, 75, 50),
        )
        self.assertEqual(c.fresh_contact_statuses, ("not_contacted", "queued_today"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
