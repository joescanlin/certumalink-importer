"""Reply-classifier interface + value types (Phase 2 task P2.2).

The classifier reads an inbound reply and labels its intent. The intent then drives a DETERMINISTIC
ledger transition (see node.py) - the model only labels; it never moves a lead itself, and it can
never reach physician_activated (the ledger-writer actor guard makes that structurally impossible).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["INTENTS", "ClassificationResult", "ReplyClassifier", "CLASSIFY_SCHEMA"]

# The closed set of intents. Keep in sync with node._EFFECTS and CLASSIFY_SCHEMA.
INTENTS = (
    "interested",      # positive: wants to proceed / claim
    "question",        # asks something answerable (escalate; we draft a reply)
    "objection",       # concern/pushback (escalate; we draft a reply)
    "not_interested",  # soft no -> stop + suppress
    "unsubscribe",     # explicit opt-out -> stop + suppress
    "out_of_office",   # auto OOO -> reschedule, do not treat as a real reply
    "auto_reply",      # other automated reply -> reschedule
    "wrong_person",    # not the physician / no longer here -> human re-routes
    "unknown",         # no confident label -> escalate to a human
)


@dataclass(frozen=True)
class ClassificationResult:
    intent: str
    confidence: float
    rationale: str = ""


class ReplyClassifier(Protocol):
    name: str

    def classify(self, *, text: str, context: str = "") -> ClassificationResult:
        ...


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}
