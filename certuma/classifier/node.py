"""The reply-classifier node (Phase 2 task P2.2).

classify_reply labels one inbound reply and applies its DETERMINISTIC effect through the single
ledger-writer (and the shared monitor suppression path), so the model's only power is to pick a
label from a closed set. The lead sits in `replied` after ingestion; this resolves it:

  interested              -> interested      (cadence will nudge toward the claim link)
  objection / question    -> needs_review    (escalate; P2.3 drafts a response a human approves)
  not_interested / unsub  -> do_not_contact  + suppress(opt_out)
  out_of_office / auto    -> awaiting_reply   + reschedule next_action_at past the OOO window
  wrong_person / unknown  -> needs_review    (a human re-routes)

It can never set physician_activated (the ledger-writer actor guard forbids any actor outside
ACTIVATION_ONLY_ACTORS), so a misfiring label cannot fake a conversion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from certuma.observability import METRICS, emit, get_logger

from .provider import ClassificationResult
from .stub import StubReplyClassifier

if TYPE_CHECKING:  # annotations only; keeps the package importable without SQLAlchemy
    from certuma.db.models import Lead, Message

__all__ = ["ClassifyOutcome", "classify_reply", "RESCHEDULE_DAYS"]

_LOG = get_logger("certuma.classifier")

RESCHEDULE_DAYS = 7  # how far to push an out-of-office lead before the next touch (provisional)

# intent -> (target_status, reason_code, suppress_opt_out)
_EFFECTS = {
    "interested": ("interested", "reply_interested", False),
    "objection": ("needs_review", "reply_objection", False),
    "question": ("needs_review", "reply_question", False),
    "wrong_person": ("needs_review", "reply_wrong_person", False),
    "unknown": ("needs_review", "reply_unknown", False),
    "not_interested": ("do_not_contact", "reply_not_interested", True),
    "unsubscribe": ("do_not_contact", "reply_unsubscribe", True),
    "out_of_office": ("awaiting_reply", "reply_out_of_office", False),
    "auto_reply": ("awaiting_reply", "reply_auto_reply", False),
}
_ESCALATED = {"needs_review"}


@dataclass(frozen=True)
class ClassifyOutcome:
    intent: str
    confidence: float
    transitioned_to: Optional[str]
    escalated: bool


def classify_reply(
    session,
    lead: "Lead",
    message: "Message",
    *,
    provider=None,
    when: Optional[datetime] = None,
    context: str = "",
) -> ClassifyOutcome:
    """Classify one inbound reply and apply its deterministic effect. Caller owns the transaction."""
    from certuma import monitor  # lazy: SQLAlchemy-dependent, only needed at call time

    provider = provider or StubReplyClassifier()
    when = when or datetime.now(timezone.utc)

    result: ClassificationResult = provider.classify(text=message.body_rendered or "", context=context)
    intent = result.intent if result.intent in _EFFECTS else "unknown"
    message.reply_classification = intent
    target, reason, suppress_opt_out = _EFFECTS[intent]

    if suppress_opt_out:
        monitor.suppress(session, reason="opt_out", npi=lead.npi, email=None, source=f"reply_{intent}")

    moved = monitor.try_transition(session, lead, target, actor="classifier", reason_code=reason)
    if moved and intent == "interested":
        lead.next_action_at = when  # cadence nudges this lead toward the claim link
    if moved and intent in ("out_of_office", "auto_reply"):
        lead.next_action_at = when + timedelta(days=RESCHEDULE_DAYS)

    session.flush()
    METRICS.incr("reply_classified", intent=intent)
    emit(_LOG, "reply_classified", lead_id=lead.id, npi=lead.npi, intent=intent,
         confidence=round(float(result.confidence), 3), to=(target if moved else None))
    return ClassifyOutcome(intent=intent, confidence=float(result.confidence),
                           transitioned_to=(target if moved else None), escalated=target in _ESCALATED)
