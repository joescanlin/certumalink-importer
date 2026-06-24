"""Support-agent node tests (Phase 4 / support). Skips without DB.

Proves the support->sales loop: a support ticket is classified, answered or escalated, and the
intent is upserted as a sales signal into the shared clinician_signal knowledge graph that sales
scoring already reads.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, func, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import intelligence, support
    from certuma.config import Settings
    from certuma.db.models import ClinicianSignal, Lead, Prospect, SupportTicket
    from certuma.support import ADVOCATE, CHURN_RISK, EXPANSION_INTENT

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
WHEN = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class SupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "support_ticket" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("migration 0011 not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _prospect(self, npi):
        self.session.add(Prospect(npi=npi, last_name="Sup", primary_specialty="Dermatology",
                                  practice_state="TX", practice_city="Austin"))
        self.session.flush()

    def _signal(self, npi, signal_type):
        return self.session.execute(
            select(ClinicianSignal).where(ClinicianSignal.npi == npi,
                                          ClinicianSignal.signal_type == signal_type,
                                          ClinicianSignal.source == "support")).scalar()

    def test_expansion_question_becomes_an_upsell_signal(self):
        self._prospect("2700000001")
        ticket, outcome = support.handle_ticket(
            self.session, npi="2700000001",
            body="We love it - can you add more seats for our whole practice?", when=WHEN)
        self.assertEqual(outcome.intent, "expansion_interest")
        self.assertEqual(outcome.sales_signal, EXPANSION_INTENT)
        self.assertFalse(outcome.escalated)
        self.assertEqual(ticket.status, "answered")
        self.assertTrue(ticket.answer)  # routine expansion gets an auto-answer
        sig = self._signal("2700000001", EXPANSION_INTENT)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.source, "support")

    def test_complaint_escalates_and_emits_a_churn_signal(self):
        self._prospect("2700000002")
        _ticket, outcome = support.handle_ticket(
            self.session, npi="2700000002",
            body="I am frustrated and want to cancel for a refund.", when=WHEN)
        self.assertEqual(outcome.intent, "complaint")
        self.assertTrue(outcome.escalated)
        self.assertEqual(outcome.sales_signal, CHURN_RISK)
        self.assertIsNotNone(self._signal("2700000002", CHURN_RISK))

    def test_satisfaction_becomes_an_advocate_signal(self):
        self._prospect("2700000003")
        _ticket, outcome = support.handle_ticket(
            self.session, npi="2700000003",
            body="I love it, this is amazing and so helpful. Thank you so much!", when=WHEN)
        self.assertEqual(outcome.sales_signal, ADVOCATE)
        self.assertIsNotNone(self._signal("2700000003", ADVOCATE))

    def test_onboarding_help_answers_without_a_sales_signal(self):
        self._prospect("2700000004")
        _ticket, outcome = support.handle_ticket(
            self.session, npi="2700000004",
            body="I need help to finish setup - where is my claim link?", when=WHEN)
        self.assertEqual(outcome.intent, "onboarding_help")
        self.assertIsNone(outcome.sales_signal)
        self.assertEqual(outcome.status, "answered")
        # no support signal rows were written for this npi
        n = self.session.execute(select(func.count()).select_from(ClinicianSignal).where(
            ClinicianSignal.npi == "2700000004", ClinicianSignal.source == "support")).scalar()
        self.assertEqual(n, 0)

    def test_signal_upsert_is_idempotent(self):
        self._prospect("2700000005")
        for _ in range(2):
            support.handle_ticket(self.session, npi="2700000005",
                                  body="We want to expand and add more seats.", when=WHEN)
        n = self.session.execute(select(func.count()).select_from(ClinicianSignal).where(
            ClinicianSignal.npi == "2700000005", ClinicianSignal.signal_type == EXPANSION_INTENT,
            ClinicianSignal.source == "support")).scalar()
        self.assertEqual(n, 1)  # two tickets, one upserted signal

    def test_upsert_update_branch_overwrites_value_and_observed_at(self):
        # the second emit must actually run the UPDATE branch body (value + observed_at), not just
        # leave the original row untouched - otherwise a stale value/timestamp would persist
        from certuma.support import emit_sales_signal
        self._prospect("2700000011")
        later = WHEN + timedelta(days=4)
        emit_sales_signal(self.session, "2700000011", EXPANSION_INTENT, value="expansion_interest", when=WHEN)
        self.session.flush()
        emit_sales_signal(self.session, "2700000011", EXPANSION_INTENT, value="feature_request", when=later)
        self.session.flush()
        sig = self._signal("2700000011", EXPANSION_INTENT)
        self.assertEqual(sig.value, "feature_request")  # value updated
        self.assertEqual(sig.observed_at, later)        # observed_at advanced
        n = self.session.execute(select(func.count()).select_from(ClinicianSignal).where(
            ClinicianSignal.npi == "2700000011", ClinicianSignal.signal_type == EXPANSION_INTENT,
            ClinicianSignal.source == "support")).scalar()
        self.assertEqual(n, 1)

    def test_support_signal_overrides_action_for_an_open_lead(self):
        # a support signal also takes precedence on a lead still in the funnel (not just activated):
        # an awaiting-reply lead flagged for churn becomes a Retention action, not the cadence action
        npi = "2700000012"
        self.session.add(Prospect(npi=npi, display_name="Dr Open", primary_specialty="Cardiology"))
        self.session.flush()
        self.session.add(Lead(npi=npi, campaign="cardiology", activation_status="awaiting_reply"))
        self.session.flush()
        support.handle_ticket(self.session, npi=npi, body="I am frustrated and want to cancel.", when=WHEN)
        self.session.flush()
        mine = [r for r in intelligence.recommended_actions(self.session, when=WHEN) if r["npi"] == npi]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["action"], "Retention outreach")

    def test_run_support_processes_only_unclassified_tickets(self):
        self._prospect("2700000006")
        self._prospect("2700000007")
        # one pre-filed open ticket, plus a handled one that should be left alone
        self.session.add(SupportTicket(npi="2700000006", body="There is a bug, 404 error on my profile.",
                                       status="open"))
        self.session.flush()
        first = support.run_support(self.session, when=WHEN)
        self.assertGreaterEqual(first.classified, 1)
        self.assertGreaterEqual(first.escalated, 1)  # the bug report escalates
        # a second pass finds nothing new (all tickets now have an intent)
        second = support.run_support(self.session, when=WHEN)
        self.assertEqual(second.classified, 0)

    def test_support_signal_surfaces_an_activated_customer_in_the_sales_queue(self):
        # the headline loop: an activated customer asks an expansion question in support, and that
        # turns into an Upsell action in the sales recommended queue (it would otherwise be "Done")
        npi = "2700000008"
        self.session.add(Prospect(npi=npi, display_name="Dr Upsell", primary_specialty="Cardiology",
                                  practice_state="TX"))
        self.session.flush()
        self.session.add(Lead(npi=npi, campaign="cardiology", activation_status="physician_activated"))
        self.session.flush()
        # before support: an activated customer is not in the recommended sales queue
        before = {r["npi"] for r in intelligence.recommended_actions(self.session, when=WHEN)}
        self.assertNotIn(npi, before)
        support.handle_ticket(self.session, npi=npi,
                              body="We love it - can you add more seats for our whole group?", when=WHEN)
        self.session.flush()
        recs = intelligence.recommended_actions(self.session, when=WHEN)
        mine = [r for r in recs if r["npi"] == npi]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["action"], "Upsell")

    def test_churn_support_signal_becomes_retention_outreach(self):
        npi = "2700000009"
        self.session.add(Prospect(npi=npi, display_name="Dr Churn", primary_specialty="Cardiology"))
        self.session.flush()
        self.session.add(Lead(npi=npi, campaign="cardiology", activation_status="physician_activated"))
        self.session.flush()
        support.handle_ticket(self.session, npi=npi,
                              body="I am frustrated and want to cancel.", when=WHEN)
        self.session.flush()
        mine = [r for r in intelligence.recommended_actions(self.session, when=WHEN) if r["npi"] == npi]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["action"], "Retention outreach")

    def test_churn_outranks_a_high_fit_routine_lead_in_the_queue(self):
        # the churn signal deliberately drives fit to ~0, but a churning customer must still sort
        # ABOVE a high-fit routine lead (urgency, not fit, orders the support actions) so it is
        # never truncated off the bottom of the queue
        from certuma import signals as sig_mod
        hi = "2700000013"
        self.session.add(Prospect(npi=hi, display_name="Dr HighFit", primary_specialty="Dermatology",
                                  practice_state="TX", practice_city="Austin"))
        self.session.flush()
        self.session.add(Lead(npi=hi, campaign="dermatology", activation_status="sendable"))
        self.session.flush()
        sig_mod.run_signal_collection(self.session, when=WHEN)  # give the routine lead a real fit score

        churn = "2700000014"
        self.session.add(Prospect(npi=churn, display_name="Dr Churn2", primary_specialty="Cardiology"))
        self.session.flush()
        self.session.add(Lead(npi=churn, campaign="cardiology", activation_status="physician_activated"))
        self.session.flush()
        support.handle_ticket(self.session, npi=churn, body="I am frustrated and want to cancel.", when=WHEN)
        self.session.flush()

        order = [r["npi"] for r in intelligence.recommended_actions(self.session, when=WHEN)]
        self.assertIn(hi, order)
        self.assertIn(churn, order)
        self.assertLess(order.index(churn), order.index(hi))  # churn floats above the high-fit lead

    def test_signal_without_npi_is_not_emitted(self):
        # an anonymous portal ticket still classifies but cannot attach a sales signal
        _ticket, outcome = support.handle_ticket(
            self.session, npi=None, body="We want to add more seats for our group.", when=WHEN)
        self.assertEqual(outcome.intent, "expansion_interest")
        self.assertEqual(outcome.sales_signal, EXPANSION_INTENT)  # the classification still stands
        n = self.session.execute(select(func.count()).select_from(ClinicianSignal).where(
            ClinicianSignal.source == "support")).scalar()
        self.assertEqual(n, 0)  # nothing written without an npi to attach it to


if __name__ == "__main__":
    unittest.main(verbosity=2)
