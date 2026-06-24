"""Orchestrator / Assisted-loop tests (Phase 1 task P1.11). Skips without DB.

Covers propose (draft -> pending Approval, suppression/dedup/lint-failure/inactive-campaign skips),
execute (approved draft -> SENDER -> email_sent, plus the precondition errors and the send-time
Gate re-check), and SLA expiry.
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
    from sqlalchemy import create_engine, func, inspect, select, text, update
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import gate, orchestrator
    from certuma.config import Settings
    from certuma.copywriter import StubCopyProvider
    from certuma.copywriter.provider import CopyOutput
    from certuma.db.models import (Approval, Campaign, Contact, Lead, Mailbox, Message,
                                   Prospect, Suppression, Template, WorkflowScore)
    from certuma.email.provider import SendResult

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
BUSINESS = datetime(2026, 6, 23, 15, tzinfo=timezone.utc)  # 10:00 CDT (TX) -> not quiet hours
CLAIM = "https://www.certumalink.com/claim/abc"
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    sender_from_title="Provider Onboarding", postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_SA else None


class CaptureEmailProvider:
    name = "capture"

    def __init__(self):
        self.outbound = None

    def send(self, email):
        self.outbound = email
        return SendResult(provider_message_id="esp-cap-1", accepted=True)


class BannedClaimCopyProvider:
    """Injects a banned claim so the linter fails and the lead routes to needs_review."""
    name = "bad"

    def draft(self, *, template_subject, template_body, facts, model="bad"):
        body = (template_body.replace("{last_name}", facts.last_name)
                .replace("{pitch_angle}", facts.pitch_angle).replace("{city}", facts.city))
        body += "\nYou are a board-certified specialist."
        return CopyOutput(subject="s", body=body, plaintext=body, variant_id="bad", merge_token_audit=())


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class OrchestratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "approval" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")
        with Session(cls.engine) as s:
            if s.get(Campaign, "dermatology") is None:
                raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _seed(self, npi="1700003001", priority="high", with_contact=True, with_mailbox=True,
              campaign="dermatology"):
        self.session.add(Prospect(npi=npi, first_name="Jane", last_name="Smith",
                                  display_name="Jane Smith MD", credential="MD",
                                  primary_specialty="Dermatology", practice_city="Austin",
                                  practice_state="TX"))
        self.session.flush()
        # an approved, campaign-agnostic template (the 0002 placeholder) + an active campaign
        self.session.execute(update(Template).where(Template.campaign.is_(None), Template.version == 1)
                             .values(is_approved=True))
        self.session.execute(update(Campaign).where(Campaign.name == campaign)
                             .values(is_active=True, is_paused=False))
        self.session.add(WorkflowScore(npi=npi, campaign="", activation_priority=priority,
                                       activation_score=80, profile_completeness_score=100,
                                       practice_group_size=5, model_version="t"))
        if with_contact:
            self.session.add(Contact(npi=npi, email=f"dr.{npi}@example.com", email_status="valid"))
        # a unique address per lead so the fixture never collides with other (possibly committed)
        # mailbox rows; deactivate any pre-existing active mailbox first so pick_mailbox is
        # deterministic for this transaction
        self.session.execute(update(Mailbox).values(is_active=False))
        if with_mailbox:
            self.session.add(Mailbox(address=f"mbx-{npi}@getcertuma.com", display_name="Jordan Avery",
                                     domain="getcertuma.com", is_active=True))
        lead = Lead(npi=npi, campaign=campaign, activation_status="sendable", claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        return lead

    def _pending(self, lead_id):
        return self.session.execute(
            select(Approval).where(Approval.lead_id == lead_id, Approval.state == "pending")
        ).scalar()

    # ---- propose ----
    def test_propose_creates_reviewable_pending_approval(self):
        lead = self._seed()
        res = orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        self.assertEqual((res.proposed, res.skipped, res.needs_review), (1, 0, 0))
        appr = self._pending(lead.id)
        self.assertIsNotNone(appr)
        self.assertEqual(appr.proposed_action, "send_email")
        self.assertEqual(appr.value_tier, "high")
        self.assertIsNotNone(appr.sla_expires_at)
        # the human reviews fully-rendered, compliant copy
        self.assertIn(CLAIM, appr.proposed_body)
        self.assertIn("getcertuma.com/u/" + lead.npi, appr.proposed_body)
        self.assertIn(SETTINGS.postal_address, appr.proposed_body)
        self.assertNotIn("{", appr.proposed_body)  # all tokens rendered
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")  # not sent yet

    def test_propose_skips_suppressed(self):
        lead = self._seed("1700003002")
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        res = orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        self.assertEqual((res.proposed, res.skipped), (0, 1))
        self.assertIsNone(self._pending(lead.id))

    def test_propose_is_idempotent_per_lead(self):
        lead = self._seed("1700003003")
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        res2 = orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        self.assertEqual(res2.proposed, 0)  # already has a pending approval
        n = self.session.execute(
            select(func.count()).select_from(Approval).where(Approval.lead_id == lead.id)
        ).scalar()
        self.assertEqual(n, 1)

    def test_propose_lint_failure_routes_to_needs_review(self):
        lead = self._seed("1700003004")
        res = orchestrator.propose_sends(self.session, provider=BannedClaimCopyProvider(),
                                         settings=SETTINGS, when=BUSINESS)
        self.assertEqual((res.proposed, res.needs_review), (0, 1))
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "needs_review")
        self.assertIsNone(self._pending(lead.id))

    def test_propose_skips_inactive_campaign(self):
        lead = self._seed("1700003005")
        self.session.execute(update(Campaign).where(Campaign.name == "dermatology").values(is_active=False))
        res = orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        self.assertEqual((res.proposed, res.skipped), (0, 1))

    # ---- execute ----
    def test_execute_approved_send_delivers_reviewed_copy(self):
        lead = self._seed("1700003006")
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        appr = self._pending(lead.id)
        appr.state = "approved"
        self.session.flush()
        email_provider = CaptureEmailProvider()
        outcome = orchestrator.execute_approved_send(
            self.session, appr, provider_email=email_provider, settings=SETTINGS, when=BUSINESS)
        self.assertTrue(outcome.sent)
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "email_sent")
        # exactly the reviewed copy went out, to the valid contact
        self.assertEqual(email_provider.outbound.to_addr, f"dr.{lead.npi}@example.com")
        self.assertEqual(email_provider.outbound.subject, appr.proposed_subject)
        self.assertIn(CLAIM, email_provider.outbound.html_body)
        # an outbound message was recorded (the idempotency key)
        msg = self.session.execute(
            select(Message).where(Message.lead_id == lead.id, Message.direction == "outbound")
        ).scalar_one()
        self.assertEqual(msg.esp_message_id, "esp-cap-1")

    def test_execute_requires_approved_state(self):
        lead = self._seed("1700003007")
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        appr = self._pending(lead.id)  # still pending
        with self.assertRaises(orchestrator.NotApproved):
            orchestrator.execute_approved_send(self.session, appr, provider_email=CaptureEmailProvider(),
                                               settings=SETTINGS, when=BUSINESS)

    def test_execute_without_valid_contact_raises(self):
        lead = self._seed("1700003008", with_contact=False)
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        appr = self._pending(lead.id)
        appr.state = "approved"
        self.session.flush()
        with self.assertRaises(orchestrator.NoValidContact):
            orchestrator.execute_approved_send(self.session, appr, provider_email=CaptureEmailProvider(),
                                               settings=SETTINGS, when=BUSINESS)

    def test_execute_without_mailbox_raises(self):
        lead = self._seed("1700003009", with_mailbox=False)
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        appr = self._pending(lead.id)
        appr.state = "approved"
        self.session.flush()
        with self.assertRaises(orchestrator.NoMailbox):
            orchestrator.execute_approved_send(self.session, appr, provider_email=CaptureEmailProvider(),
                                               settings=SETTINGS, when=BUSINESS)

    def test_execute_re_checks_gate_and_does_not_send_when_suppressed(self):
        lead = self._seed("1700003010")
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS, when=BUSINESS)
        appr = self._pending(lead.id)
        appr.state = "approved"
        # opt-out lands AFTER approval but BEFORE the send fires
        self.session.add(Suppression(npi=lead.npi, reason="opt_out"))
        self.session.flush()
        email_provider = CaptureEmailProvider()
        outcome = orchestrator.execute_approved_send(
            self.session, appr, provider_email=email_provider, settings=SETTINGS, when=BUSINESS)
        self.assertFalse(outcome.sent)
        self.assertTrue(outcome.terminal)  # BLOCK (suppression)
        self.assertEqual(outcome.decision.reason_code, "suppression")
        self.assertIsNone(email_provider.outbound)  # nothing left the building
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "sendable")  # unchanged

    # ---- SLA ----
    def test_expire_stale_approvals(self):
        lead = self._seed("1700003011")
        orchestrator.propose_sends(self.session, provider=StubCopyProvider(), settings=SETTINGS,
                                   when=BUSINESS - timedelta(hours=48))  # SLA already elapsed
        n = orchestrator.expire_stale_approvals(self.session, when=BUSINESS)
        self.assertEqual(n, 1)
        appr = self.session.execute(select(Approval).where(Approval.lead_id == lead.id)).scalar_one()
        self.assertEqual(appr.state, "expired")


if __name__ == "__main__":
    unittest.main(verbosity=2)
