"""Stub LinkedIn channel (Phase 3 task P3.8) - the second channel behind the seam.

LinkedIn outreach is not CAN-SPAM email, so it does NOT run the email Gate (no postal/unsubscribe
footer, no quiet-hours-by-mailbox); it DOES honor suppression (a do-not-contact clinician is never
touched on any channel) and records the touch through the single ledger-writer with the shared
(npi, campaign, cadence_step) idempotency key, so a step is sent at most once across channels. The
actual LinkedIn API call is stubbed; a real connector slots in behind this same interface later.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from certuma import gate, ledger_writer
from certuma.db.models import Message
from certuma.observability import METRICS, emit, get_logger

from .provider import ChannelResult

__all__ = ["StubLinkedInChannel"]

_LOG = get_logger("certuma.channels.linkedin")


class StubLinkedInChannel:
    name = "linkedin"

    def send(self, session, lead, *, content: str = "", settings=None, when: Optional[datetime] = None,
             **_) -> ChannelResult:
        when = when or datetime.now(timezone.utc)
        # honor the channel-agnostic operational controls (suppression, kill switch, campaign pause);
        # the email-specific Gate checks (CAN-SPAM, quiet hours, warmup, breakers) do not apply here.
        hold = gate.operational_hold(session, npi=lead.npi, campaign=lead.campaign)
        if hold:
            return ChannelResult(sent=False, channel="linkedin", reason=hold)

        idem = dict(
            lead_id=lead.id, npi=lead.npi, campaign=lead.campaign, cadence_step=lead.cadence_step,
            direction="outbound", channel="linkedin", variant_id="linkedin",
            body_rendered=content or "(LinkedIn message)",
        )
        ledger_writer.transition(session, lead.id, "email_sent", actor="linkedin",
                                 reason_code="linkedin_touch", expected_version=lead.version,
                                 idempotency=idem)
        msg = session.execute(
            select(Message).where(
                Message.npi == lead.npi, Message.campaign == lead.campaign,
                Message.cadence_step == lead.cadence_step, Message.direction == "outbound")
        ).scalar_one()
        msg.sent_at = when  # stub "send" - no external API call
        session.flush()
        METRICS.incr("channel_send", channel="linkedin")
        emit(_LOG, "linkedin_touch", lead_id=lead.id, npi=lead.npi, message_id=msg.id)
        return ChannelResult(sent=True, channel="linkedin", message_id=msg.id)
