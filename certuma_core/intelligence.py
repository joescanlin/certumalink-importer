"""Trigger-signal fit scoring + Recommended Actions (Phase 3 task P3.4) - pure, no DB.

fit_score folds the knowledge-graph signals (group size, public message-burden, activity, panel
size) into a 0-100 fit score, applying signal confidence and recency decay so stale or low-confidence
signals count for less (latent issue 4). recommend_action maps a lead's state to its next-best-action.
Together they are the proposal's "score and rank by fit + trigger signals" + "Recommended Actions";
the DB wrapper (certuma/intelligence.py) loads signals and ranks open leads top-fit first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

__all__ = [
    "SignalView", "fit_score", "fit_tier", "recommend_action", "support_action",
    "RECENCY_HALFLIFE_DAYS", "GROUP_CAP", "PANEL_CAP", "SUPPORT_ACTION_MAX_AGE_DAYS",
]

# signal-type keys (mirror certuma.signals.provider; duplicated here to keep this module pure)
_GROUP_SIZE = "group_size"
_MESSAGE_BURDEN = "message_burden"
_PUBLIC_ACTIVITY = "public_activity"
_PANEL_SIZE = "panel_size"

# support-derived sales signals (mirror certuma.support.provider) - the support agents write these
# into the same knowledge graph, so they fold straight into fit scoring and the recommended queue.
_EXPANSION = "expansion_intent"
_ADVOCATE = "advocate"
_CHURN_SUPPORT = "churn_risk_support"

GROUP_CAP = 20.0
PANEL_CAP = 5000.0
RECENCY_HALFLIFE_DAYS = 180.0  # a signal this old counts at ~half weight (linear floor 0.3)
SUPPORT_ACTION_MAX_AGE_DAYS = 90.0  # a support signal older than this no longer drives a sales action


@dataclass(frozen=True)
class SignalView:
    value: str = ""
    numeric: Optional[float] = None
    confidence: float = 1.0
    age_days: float = 0.0


def _weight(sig: SignalView) -> float:
    recency = max(0.3, 1.0 - (sig.age_days / RECENCY_HALFLIFE_DAYS))
    return max(0.0, min(1.0, sig.confidence)) * recency


def fit_score(signals: Dict[str, SignalView]) -> int:
    """0-100 fit score from the knowledge-graph signals. Missing signals contribute nothing.

    Support-derived signals shift the score so support interactions genuinely move sales priority:
    an expansion question or a happy customer lifts fit, a support-flagged churn risk lowers it.
    """
    score = 0.0
    g = signals.get(_GROUP_SIZE)
    if g and g.numeric is not None:
        score += min(g.numeric, GROUP_CAP) / GROUP_CAP * 25.0 * _weight(g)
    b = signals.get(_MESSAGE_BURDEN)
    if b and b.numeric is not None:
        score += min(b.numeric, 100.0) / 100.0 * 30.0 * _weight(b)
    a = signals.get(_PUBLIC_ACTIVITY)
    if a:
        score += (20.0 if a.value == "active" else 5.0) * _weight(a)
    p = signals.get(_PANEL_SIZE)
    if p and p.numeric is not None:
        score += min(p.numeric, PANEL_CAP) / PANEL_CAP * 25.0 * _weight(p)
    # support-derived sales signals (bounded; a churn flag pulls fit down)
    for key, points in ((_EXPANSION, 22.0), (_ADVOCATE, 12.0), (_CHURN_SUPPORT, -25.0)):
        s = signals.get(key)
        if s:
            score += points * _weight(s)
    return int(round(max(0.0, min(score, 100.0))))


def _actionable(sig: Optional[SignalView]) -> bool:
    """A support signal warrants a sales action only while it is recent and carries confidence -
    otherwise a years-old support touch would re-admit a customer to the queue forever."""
    return (sig is not None and sig.confidence > 0.0
            and sig.age_days <= SUPPORT_ACTION_MAX_AGE_DAYS)


def support_action(signals: Dict[str, SignalView]) -> Optional[Tuple[str, str, int]]:
    """The sales next-best-action a recent support signal implies, with an urgency rank used to order
    the queue (higher = more urgent). Churn outranks upsell outranks referral - handle the unhappy
    customer first - and that ordering, not raw fit, is what should float these to the top. Stale or
    zero-confidence signals no longer drive an action. Returns (action, reason, urgency) or None."""
    if _actionable(signals.get(_CHURN_SUPPORT)):
        return ("Retention outreach", "churn risk flagged in support", 2)
    if _actionable(signals.get(_EXPANSION)):
        return ("Upsell", "expansion interest from support", 1)
    if _actionable(signals.get(_ADVOCATE)):
        return ("Ask for referral", "happy customer (support advocate)", 1)
    return None


def fit_tier(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 35 else "low"


_DONE = {"physician_activated": "activated", "do_not_contact": "closed (do not contact)",
         "exhausted": "closed (exhausted)"}


def recommend_action(lead_status: str, *, has_contact: bool, due_now: bool) -> Tuple[str, str]:
    """The next-best-action (action, reason) for a lead given its state. Pure."""
    if lead_status in ("not_contacted", "queued_today"):
        return ("Enrich", "no deliverable contact yet") if not has_contact else ("Send first touch", "ready to send")
    if lead_status == "enriching":
        return ("Enrich", "finding a deliverable contact")
    if lead_status == "sendable":
        return ("Send first touch", "enriched and ready")
    if lead_status == "email_sent":
        return ("Await delivery", "sent, awaiting delivery")
    if lead_status in ("awaiting_reply", "interested"):
        return ("Send follow-up", "cadence due") if due_now else ("Wait", "awaiting reply / claim")
    if lead_status == "replied":
        return ("Classify reply", "a reply is waiting")
    if lead_status == "needs_review":
        return ("Review", "needs a human")
    if lead_status in _DONE:
        return ("Done", _DONE[lead_status])
    return ("Review", "unexpected state")
