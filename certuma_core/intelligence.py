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
    "SignalView", "fit_score", "fit_tier", "recommend_action", "RECENCY_HALFLIFE_DAYS",
    "GROUP_CAP", "PANEL_CAP",
]

# signal-type keys (mirror certuma.signals.provider; duplicated here to keep this module pure)
_GROUP_SIZE = "group_size"
_MESSAGE_BURDEN = "message_burden"
_PUBLIC_ACTIVITY = "public_activity"
_PANEL_SIZE = "panel_size"

GROUP_CAP = 20.0
PANEL_CAP = 5000.0
RECENCY_HALFLIFE_DAYS = 180.0  # a signal this old counts at ~half weight (linear floor 0.3)


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
    """0-100 fit score from the knowledge-graph signals. Missing signals contribute nothing."""
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
    return int(round(min(score, 100.0)))


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
