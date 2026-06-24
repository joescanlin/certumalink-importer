"""Reply drafter (Phase 2 task P2.3).

When the classifier labels an inbound reply an objection or a question it routes the lead to
needs_review (decision 4: objections are ALWAYS human-reviewed). This node drafts a suggested
response with the reply_drafter agent (Opus) and files it as a `reply` Approval carrying the
escalation reason, so the human sees a ready-to-edit answer in the Escalations queue rather than a
blank box. The draft is never sent automatically; a human approves it.

A deterministic stub backs the tests and the dev loop; the real Opus draft uses the active
reply_drafter prompt from the Agent Studio. Compliance tokens are injected deterministically and a
presence guard runs (unsubscribe + postal), the same last-line check the SENDER applies; the full
hallucination linter is intentionally not run here (it is tuned for templated copy, and a human
reviews every reply anyway). Caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.config import Settings, get_settings
from certuma.db.models import Approval, Lead, Message, Prospect
from certuma.observability import METRICS, emit, get_logger
from certuma_core import campaigns, urls

__all__ = ["DraftedReply", "ReplyDraftSummary", "StubReplyDraftProvider", "draft_pending_replies",
           "DRAFTABLE_INTENTS"]

_LOG = get_logger("certuma.reply_drafter")

DRAFTABLE_INTENTS = ("objection", "question")


@dataclass(frozen=True)
class DraftedReply:
    subject: str
    body: str


class StubReplyDraftProvider:
    """Deterministic suggested response; leaves the three compliance tokens literal for render."""
    name = "stub"

    def draft(self, *, objection: str, last_name: str, specialty: str, city: str, intent: str) -> DraftedReply:
        who = f"Dr. {last_name}" if last_name else "there"
        topic = specialty or "your practice"
        body = (
            f"Hi {who}, thanks for getting back to me. Happy to clarify: this is a draft profile we "
            f"prepared from public directory information for {topic}"
            + (f" in {city}" if city else "")
            + ". There is no cost and no obligation - you can review the details and claim or correct "
            "the profile here: {claim_url}. If you would prefer not to hear from us, you can "
            "unsubscribe here: {unsubscribe_url}. {postal_address}"
        )
        return DraftedReply(subject="Re: your profile", body=body)


@dataclass
class ReplyDraftSummary:
    drafted: int = 0
    skipped: int = 0
    drafted_lead_ids: List[int] = field(default_factory=list)


def _latest_inbound(session: Session, lead_id: int) -> Optional[Message]:
    return session.execute(
        select(Message).where(Message.lead_id == lead_id, Message.direction == "inbound")
        .order_by(Message.id.desc()).limit(1)
    ).scalar()


def _has_pending_reply_approval(session: Session, lead_id: int) -> bool:
    return session.execute(
        select(Approval.id).where(Approval.lead_id == lead_id, Approval.proposed_action == "reply",
                                  Approval.state == "pending").limit(1)
    ).first() is not None


def draft_pending_replies(
    session: Session,
    *,
    provider=None,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
    limit: int = 200,
) -> ReplyDraftSummary:
    """Draft a suggested response for each needs_review lead whose last reply needs one. Caller commits."""
    settings = settings or get_settings()
    provider = provider or StubReplyDraftProvider()
    when = when or datetime.now(timezone.utc)
    domain = settings.cold_domain or "localhost"

    leads = session.execute(
        select(Lead).where(Lead.activation_status == "needs_review").order_by(Lead.id).limit(limit)
    ).scalars().all()

    summary = ReplyDraftSummary()
    for lead in leads:
        inbound = _latest_inbound(session, lead.id)
        if inbound is None or inbound.reply_classification not in DRAFTABLE_INTENTS:
            continue
        if _has_pending_reply_approval(session, lead.id):
            continue

        prospect = session.get(Prospect, lead.npi)
        preset = campaigns.CAMPAIGN_PRESETS.get(lead.campaign)
        specialty = getattr(prospect, "primary_specialty", "") or (preset.pitch_angle if preset else "")
        drafted = provider.draft(
            objection=inbound.body_rendered or "",
            last_name=getattr(prospect, "last_name", "") or "",
            specialty=specialty, city=getattr(prospect, "practice_city", "") or "",
            intent=inbound.reply_classification,
        )
        unsubscribe_url = urls.unsubscribe_url(domain, lead.npi)
        body = (drafted.body.replace("{claim_url}", lead.claim_url or "")
                .replace("{unsubscribe_url}", unsubscribe_url)
                .replace("{postal_address}", settings.postal_address))

        # last-line compliance presence guard (a human reviews the rest)
        if (unsubscribe_url not in body) or (settings.postal_address and settings.postal_address not in body):
            summary.skipped += 1
            continue

        session.add(Approval(lead_id=lead.id, proposed_action="reply",
                             gate_reason_code=inbound.reply_classification,
                             proposed_subject=drafted.subject, proposed_body=body, state="pending"))
        summary.drafted += 1
        summary.drafted_lead_ids.append(lead.id)
        METRICS.incr("reply_drafted", intent=inbound.reply_classification)
        emit(_LOG, "reply_drafted", lead_id=lead.id, npi=lead.npi, intent=inbound.reply_classification)

    session.flush()
    return summary
