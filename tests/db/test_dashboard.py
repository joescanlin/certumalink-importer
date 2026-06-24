"""Dashboard skeleton tests (Phase 0 tasks B14/B15).

Uses the join-an-external-transaction pattern: the app's get_db dependency is overridden with a
session bound to a connection whose outer transaction is rolled back in tearDown, so the handlers
may commit (savepoint release) without ever mutating the real DB. Skips when DB/SQLAlchemy/FastAPI
are absent. The load-bearing assertion: toggling the kill switch / pausing a campaign via the API
changes what the Gate returns.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import Session
    HAVE_DEPS = True
except Exception:  # pragma: no cover
    HAVE_DEPS = False

if HAVE_DEPS:
    from certuma.config import Settings
    from certuma.api.app import create_app, get_db
    from certuma.db.models import (Approval, Campaign, Contact, Event, Lead, Mailbox, Message,
                                   Prospect, Suppression, Thread)
    from certuma.email.provider import SendResult

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_DEPS else "")
CLAIM = "https://www.certumalink.com/claim/abc"
SETTINGS = Settings(
    sender_from_email="jordan@getcertuma.com", sender_from_name="Jordan Avery",
    sender_from_title="Provider Onboarding", postal_address="Certuma, 1 Main St, Austin TX 78701",
    cold_domain="getcertuma.com", reply_to_domain="getcertuma.com",
) if HAVE_DEPS else None


class CaptureEmailProvider:
    name = "capture"

    def __init__(self):
        self.outbound = None

    def send(self, email):
        self.outbound = email
        return SendResult(provider_message_id="esp-dash-1", accepted=True)


@unittest.skipUnless(HAVE_DEPS, "SQLAlchemy/FastAPI not installed")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "lead" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")
        with Session(cls.engine) as s:
            if s.get(Campaign, "dermatology") is None:
                raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_DEPS and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.trans = self.conn.begin()
        self.session = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        self.app = create_app()
        self.app.dependency_overrides[get_db] = self._override
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.session.close()
        self.trans.rollback()
        self.conn.close()

    def _override(self):
        yield self.session

    def _seed_approval(self, npi="1000000001"):
        self.session.add(Prospect(npi=npi, display_name="Dr Test"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="legacy", activation_status="sendable")
        self.session.add(lead)
        self.session.flush()
        appr = Approval(lead_id=lead.id, proposed_action="first_touch", value_tier="high",
                        gate_reason_code="value_tier", state="pending")
        self.session.add(appr)
        self.session.flush()
        return appr.id

    # ---- read views ----
    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("leads", "prospects", "campaigns", "pending_approvals", "kill_switch_active", "status_counts"):
            self.assertIn(key, body)
        self.assertGreaterEqual(body["campaigns"], 6)  # seeded
        self.assertFalse(body["kill_switch_active"])

    def test_approvals_empty_then_seeded(self):
        self.assertEqual(self.client.get("/approvals").json(), [])
        aid = self._seed_approval()
        rows = self.client.get("/approvals?state=pending").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["display_name"], "Dr Test")
        self.assertEqual(rows[0]["gate_reason_code"], "value_tier")

    def test_decision_moves_approval(self):
        aid = self._seed_approval()
        r = self.client.post(f"/approvals/{aid}/decision", json={"decision": "approved"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "approved")
        self.assertEqual(self.client.get("/approvals?state=pending").json(), [])
        self.assertEqual(len(self.client.get("/approvals?state=approved").json()), 1)

    def test_decision_rejects_bad_value(self):
        aid = self._seed_approval()
        self.assertEqual(self.client.post(f"/approvals/{aid}/decision", json={"decision": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/approvals/999999/decision", json={"decision": "approved"}).status_code, 404)

    # ---- the load-bearing wiring: switch toggles change the Gate ----
    def test_kill_switch_toggles_gate(self):
        self.assertEqual(self.client.get("/gate/preview", params={"npi": "1000000001"}).json()["decision"], "ALLOW")
        self.assertEqual(self.client.post("/kill-switch", json={"active": True}).json()["kill_switch_active"], True)
        g = self.client.get("/gate/preview", params={"npi": "1000000001"}).json()
        self.assertEqual((g["decision"], g["reason_code"]), ("HOLD", "kill_switch"))
        self.client.post("/kill-switch", json={"active": False})
        self.assertEqual(self.client.get("/gate/preview", params={"npi": "1000000001"}).json()["decision"], "ALLOW")

    def test_campaign_pause_toggles_gate(self):
        self.assertEqual(self.client.post("/campaigns/dermatology/pause", json={"paused": True}).status_code, 200)
        g = self.client.get("/gate/preview", params={"npi": "1000000001", "campaign": "dermatology"}).json()
        self.assertEqual((g["decision"], g["reason_code"]), ("HOLD", "campaign_paused"))
        self.assertEqual(self.client.post("/campaigns/nope/pause", json={"paused": True}).status_code, 404)

    # ---- styled console ----
    def test_index_renders_styled_console(self):
        self._seed_approval()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        for marker in ("Certuma Reach", "Approvals", "/static/certuma.css", "Plus+Jakarta+Sans",
                       "Dr Test", "Approve"):
            self.assertIn(marker, r.text)

    def test_stylesheet_is_served(self):
        r = self.client.get("/static/certuma.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/css", r.headers["content-type"])
        self.assertIn("--accent: #2d5b6e", r.text)  # the certumalink teal token

    # ---- Approve wires the orchestrator ----
    def test_approve_without_contact_reports_orchestrator_error(self):
        # the seeded approval has no contact/mailbox: execute_approved_send must run and surface it
        aid = self._seed_approval("1000000002")
        send = self.client.post(f"/approvals/{aid}/decision", json={"decision": "approved"}).json()["send"]
        self.assertEqual(send, {"sent": False, "error": "NoValidContact"})

    def test_approve_full_seed_runs_full_send_path(self):
        npi = "1000000003"
        self.session.add(Prospect(npi=npi, display_name="Dr Send", first_name="Sam", last_name="Send",
                                  primary_specialty="Dermatology", practice_city="Austin", practice_state="TX"))
        self.session.flush()
        # the Gate's CAN-SPAM check needs an approved template configured for the campaign
        from sqlalchemy import update as _update
        from certuma.db.models import Template
        self.session.execute(_update(Template).where(Template.campaign.is_(None), Template.version == 1)
                             .values(is_approved=True))
        self.session.add(Contact(npi=npi, email=f"dr.{npi}@example.com", email_status="valid"))
        self.session.add(Mailbox(address=f"mbx-{npi}@getcertuma.com", domain="getcertuma.com", is_active=True))
        lead = Lead(npi=npi, campaign="dermatology", activation_status="sendable", claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        body = (f"Hi Dr Send, review your dermatology profile: {CLAIM}. "
                f"Unsubscribe: https://getcertuma.com/u/{npi}. {SETTINGS.postal_address}")
        appr = Approval(lead_id=lead.id, proposed_action="send_email", value_tier="high",
                        proposed_subject="Your dermatology profile", proposed_body=body, state="pending")
        self.session.add(appr)
        self.session.flush()

        capture = CaptureEmailProvider()
        app = create_app(settings=SETTINGS, email_provider=capture)
        app.dependency_overrides[get_db] = self._override
        client = TestClient(app)
        try:
            send = client.post(f"/approvals/{appr.id}/decision", json={"decision": "approved"}).json()["send"]
        finally:
            client.close()
        self.assertIsNotNone(send)
        self.assertIn("sent", send)
        if send["sent"]:
            self.assertIsNotNone(capture.outbound)
            self.assertEqual(capture.outbound.to_addr, f"dr.{npi}@example.com")
            self.session.refresh(lead)
            self.assertEqual(lead.activation_status, "email_sent")
        else:
            # the only non-suppressed HOLD possible for this clean lead is quiet hours (wall clock)
            self.assertEqual(send["reason_code"], "quiet_hours")

    # ---- kill-switch control on the shell ----
    def test_kill_toggle_control_reflects_state(self):
        page = self.client.get("/").text
        self.assertIn("toggleKill(true)", page)        # offer to pause when clear
        self.assertIn("Pause all sending", page)
        self.client.post("/kill-switch", json={"active": True})
        page = self.client.get("/").text
        self.assertIn("toggleKill(false)", page)       # offer to resume when active
        self.assertIn("Resume sending", page)
        self.assertIn("banner live", page)             # the active-kill banner shows

    # ---- campaigns screen ----
    def test_campaigns_page_renders(self):
        r = self.client.get("/campaigns")
        self.assertEqual(r.status_code, 200)
        for marker in ("Campaigns", "Autonomy", "dermatology", "campaignSet", "Activate"):
            self.assertIn(marker, r.text)

    def test_campaign_config_endpoint_updates(self):
        r = self.client.post("/campaigns/dermatology",
                             json={"is_active": True, "autonomy_level": "supervised"})
        self.assertEqual(r.status_code, 200)
        camp = self.session.get(Campaign, "dermatology")
        self.session.refresh(camp)
        self.assertTrue(camp.is_active)
        self.assertEqual(camp.autonomy_level, "supervised")

    def test_campaign_config_validates(self):
        self.assertEqual(
            self.client.post("/campaigns/dermatology", json={"autonomy_level": "wat"}).status_code, 400)
        self.assertEqual(self.client.post("/campaigns/dermatology", json={}).status_code, 400)
        self.assertEqual(self.client.post("/campaigns/nope", json={"is_active": True}).status_code, 404)

    # ---- template studio ----
    def test_studio_page_renders(self):
        r = self.client.get("/studio")
        self.assertEqual(r.status_code, 200)
        for marker in ("Template studio", "Run linter", "lintTemplate(", "approveTemplate("):
            self.assertIn(marker, r.text)
        self.assertIn("draft", r.text)  # the seeded placeholder template awaits approval

    # ---- activity / funnel ----
    def test_activity_page_renders_funnel(self):
        from datetime import datetime, timezone
        npi = "1000000005"
        self.session.add(Prospect(npi=npi, display_name="Dr Funnel"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="physician_activated")
        self.session.add(lead)
        self.session.flush()
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=1,
                                 direction="outbound", subject="s", delivered=True))
        self.session.add(Suppression(npi=npi, reason="opt_out"))
        self.session.add(Event(dedup_key="act-ev-1", npi=npi, event_type="delivered",
                               occurred_at=datetime(2026, 6, 23, 15, tzinfo=timezone.utc)))
        self.session.flush()
        r = self.client.get("/activity")
        self.assertEqual(r.status_code, 200)
        for marker in ("Conversion funnel", "Emails sent", "Recent events", "Lead status",
                       "physician_activated", "opt_out", "delivered"):
            self.assertIn(marker, r.text)

    # ---- Agent Studio (Phase 2) ----
    def test_agents_page_renders_workflow_and_roster(self):
        r = self.client.get("/agents")
        self.assertEqual(r.status_code, 200)
        for marker in ("Agent Studio", "Workflow", "Copywriter", "Reply Classifier",
                       "Compliance Gate", "Claim Poller", "Spin up a fresh agent",
                       "saveAgent(", "Haiku"):
            self.assertIn(marker, r.text)

    def test_agent_create_update_activate_endpoints(self):
        from certuma.db.models import Agent
        created = self.client.post("/agents", json={
            "role": "copywriter", "name": "Warm derm", "model": "claude-sonnet-4-6",
            "system_prompt": "Be warm and concise.", "activate": True}).json()
        aid = created["id"]
        self.assertTrue(created["is_active"])
        # update bumps the version
        upd = self.client.post(f"/agents/{aid}", json={"system_prompt": "Be warmer."}).json()
        self.assertEqual(upd["version"], 2)
        a = self.session.get(Agent, aid)
        self.session.refresh(a)
        self.assertEqual(a.system_prompt, "Be warmer.")
        # activate is idempotent + 404s on a missing agent
        self.assertEqual(self.client.post(f"/agents/{aid}/activate").status_code, 200)
        self.assertEqual(self.client.post("/agents/999999/activate").status_code, 404)

    def test_agent_create_validates(self):
        self.assertEqual(self.client.post("/agents", json={
            "role": "nope", "name": "x", "system_prompt": "y"}).status_code, 400)

    # ---- inbound reply webhook (Phase 2) ----
    def test_inbound_reply_classifies_and_transitions(self):
        npi = "1000000006"
        self.session.add(Prospect(npi=npi, display_name="Dr Reply", practice_state="TX"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="awaiting_reply",
                    claim_url=CLAIM)
        self.session.add(lead)
        self.session.flush()
        self.session.add(Thread(lead_id=lead.id, reply_token="rtok-6"))
        self.session.add(Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=0,
                                 direction="outbound", subject="s", esp_message_id="o-6"))
        self.session.flush()
        r = self.client.post("/inbound/reply", json={
            "reply_token": "rtok-6", "text": "Yes, I'd like to claim my profile",
            "esp_message_id": "reply-6"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["matched"])
        self.assertEqual(body["intent"], "interested")
        self.assertEqual(body["transitioned_to"], "interested")
        self.session.refresh(lead)
        self.assertEqual(lead.activation_status, "interested")

    # ---- inbound event webhook ----
    def test_event_webhook_drives_lifecycle(self):
        npi = "1000000004"
        self.session.add(Prospect(npi=npi, display_name="Dr Event"))
        self.session.flush()
        lead = Lead(npi=npi, campaign="dermatology", activation_status="email_sent")
        self.session.add(lead)
        self.session.flush()
        msg = Message(lead_id=lead.id, npi=npi, campaign="dermatology", cadence_step=1,
                      direction="outbound", subject="s")
        self.session.add(msg)
        self.session.flush()

        r = self.client.post("/events/email", json={
            "event_type": "delivered", "dedup_key": "wh-d-1", "message_id": msg.id,
            "occurred_at": "2026-06-23T15:00:00+00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["transitioned_to"], "awaiting_reply")
        # redelivery is deduped
        r2 = self.client.post("/events/email", json={
            "event_type": "delivered", "dedup_key": "wh-d-1", "message_id": msg.id})
        self.assertTrue(r2.json()["duplicate"])
        # an opt-out suppresses + stops the lead
        r3 = self.client.post("/events/email", json={
            "event_type": "opt_out", "dedup_key": "wh-o-1", "npi": npi})
        self.assertEqual(r3.json()["transitioned_to"], "do_not_contact")
        self.assertTrue(r3.json()["suppressed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
