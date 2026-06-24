"""Support-agent interfaces + the intent -> sales-signal map (Phase 4 / support). Pure.

The support agent classifies an onboarding/support message, and each intent maps to (a) how the
support side handles it (auto-answer vs escalate to a human) and (b) a SALES signal written back to
the shared clinician_signal knowledge graph - so support work compounds into sales intelligence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

__all__ = [
    "SUPPORT_INTENTS", "SUPPORT_SCHEMA", "SupportClassification", "SupportClassifier", "EFFECTS",
    "EXPANSION_INTENT", "ADVOCATE", "CHURN_RISK", "SUPPORT_SIGNALS",
]

SUPPORT_INTENTS = (
    "expansion_interest",  # wants more seats/locations -> upsell lead for sales
    "feature_request",     # wants a capability -> product + a soft expansion signal
    "satisfaction",        # happy -> referral/advocate lead for sales
    "complaint",           # unhappy -> churn risk
    "bug_report",          # something broken -> churn risk + escalate
    "billing",             # billing question -> answer
    "onboarding_help",     # setup/claim help -> answer
    "how_to",              # general how-to -> answer
    "other",               # escalate
)

# sales-signal types written to clinician_signal (source='support')
EXPANSION_INTENT = "expansion_intent"
ADVOCATE = "advocate"
CHURN_RISK = "churn_risk_support"
SUPPORT_SIGNALS = (EXPANSION_INTENT, ADVOCATE, CHURN_RISK)

# intent -> (resolved status, sales_signal or None, auto_answer text or None)
EFFECTS = {
    "expansion_interest": ("answered", EXPANSION_INTENT,
                           "Wonderful - I will have our team reach out about expanding your setup."),
    "feature_request": ("escalated", EXPANSION_INTENT, None),
    "satisfaction": ("answered", ADVOCATE,
                     "Thank you so much - glad it is working well for you!"),
    "complaint": ("escalated", CHURN_RISK, None),
    "bug_report": ("escalated", CHURN_RISK, None),
    "billing": ("answered", None,
                "Happy to help with billing - your plan and invoices are on the Billing tab; "
                "reply here with the specifics and we will sort it out."),
    "onboarding_help": ("answered", None,
                        "To finish setup, open your claim link and confirm your profile details; "
                        "I can walk you through any step."),
    "how_to": ("answered", None,
               "Happy to help - here is how to do that, and reply if anything is unclear."),
    "other": ("escalated", None, None),
}


@dataclass(frozen=True)
class SupportClassification:
    intent: str
    confidence: float
    rationale: str = ""


class SupportClassifier(Protocol):
    name: str

    def classify(self, *, text: str, context: str = "") -> SupportClassification:
        ...


SUPPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(SUPPORT_INTENTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}
