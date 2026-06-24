"""Autonomy policy (Phase 2 task P2.6) - pure, no DB.

Decides, per campaign autonomy level and lead value, whether a proposed send fires automatically or
is escalated to the human. This is the switch that turns the Assisted loop into a self-running one:

  assisted    every send is escalated (Phase 1 behavior - a human approves each)
  supervised  routine sends auto-fire; HIGH-value leads are escalated for a human look
  autonomous  all sends auto-fire (within the guardrails the Gate already enforces)

No model-confidence input is needed: a proposal only exists once the copywriter produced a
lint-clean draft (draft.ok), so the quality bar is already met; the policy adds the value/autonomy
judgement on top. Objection and question REPLIES are a separate path - the classifier always routes
those to needs_review, so they are never auto-answered regardless of this policy.
"""
from __future__ import annotations

from typing import Optional

__all__ = ["AUTO_SEND", "ESCALATE", "AUTONOMY_LEVELS", "decide", "ESCALATE_TIERS"]

AUTO_SEND = "auto_send"
ESCALATE = "escalate"

AUTONOMY_LEVELS = ("assisted", "supervised", "autonomous")

# Under 'supervised', these value tiers are escalated rather than auto-sent.
ESCALATE_TIERS = frozenset({"high"})


def decide(autonomy_level: str, value_tier: Optional[str]) -> str:
    """Return AUTO_SEND or ESCALATE for a proposed send. Unknown autonomy is treated as 'assisted'."""
    if autonomy_level == "autonomous":
        return AUTO_SEND
    if autonomy_level == "supervised":
        return ESCALATE if (value_tier or "").lower() in ESCALATE_TIERS else AUTO_SEND
    return ESCALATE  # assisted / unknown: always a human
