"""StubReplyClassifier (Phase 2 task P2.2) - deterministic keyword rules, no LLM.

Backs the tests and is a safe fallback. Order matters: the strongest/safest signals
(unsubscribe, not-interested, OOO) are checked before the softer ones so a mixed message resolves
to the more conservative intent.
"""
from __future__ import annotations

from .provider import ClassificationResult

__all__ = ["StubReplyClassifier"]

_RULES = (
    ("unsubscribe", 0.99, ("unsubscribe", "stop emailing", "remove me", "take me off", "opt out", "opt-out")),
    ("not_interested", 0.9, ("not interested", "no thanks", "no thank you", "not a fit", "please stop", "no interest")),
    ("out_of_office", 0.95, ("out of office", "ooo", "on leave", "away until", "annual leave",
                             "vacation", "maternity leave", "parental leave", "auto-reply", "autoreply")),
    ("wrong_person", 0.85, ("wrong person", "not me", "no longer here", "no longer with", "retired",
                            "left the practice", "you have the wrong", "i am not")),
    ("objection", 0.8, ("how much", "cost", "pricing", "price", "concern", "worried", "hipaa",
                        "is this legit", "is this real", "scam", "why are you", "who gave you", "spam")),
    ("interested", 0.85, ("interested", "sounds good", "let's", "lets do", "sign me up", "set me up",
                          "i'd like", "i would like", "happy to", "yes please", "go ahead", "claim")),
)


class StubReplyClassifier:
    name = "stub"

    def classify(self, *, text: str, context: str = "") -> ClassificationResult:
        t = (text or "").lower()
        for intent, conf, keys in _RULES:
            if any(k in t for k in keys):
                return ClassificationResult(intent=intent, confidence=conf, rationale=f"matched {intent} keyword")
        if "?" in t:
            return ClassificationResult(intent="question", confidence=0.7, rationale="contains a question")
        return ClassificationResult(intent="unknown", confidence=0.3, rationale="no rule matched")
