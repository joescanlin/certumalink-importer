"""Deterministic support classifier (Phase 4 / support) - keyword rules, no LLM."""
from __future__ import annotations

from .provider import SupportClassification

__all__ = ["StubSupportClassifier"]

# (intent, confidence, keywords) - order matters: stronger/safer signals first. Complaint keywords
# are phrased to avoid stealing benign billing/how-to text ('a refund' not bare 'refund' so it does
# not match a billing 'refunded'; 'competitor' not bare 'switch to' so it does not match 'switch to
# dark mode').
_RULES = (
    ("complaint", 0.9, ("frustrated", "cancel", "a refund", "refund me", "terrible", "disappointed",
                        "angry", "unacceptable", "competitor")),
    ("bug_report", 0.88, ("bug", "error", "broken", "crash", "not loading", "glitch", "does not work",
                          "doesn't work", "404")),
    ("expansion_interest", 0.85, ("add more", "more seats", "another location", "second location",
                                  "upgrade", "expand", "more providers", "our group", "whole practice",
                                  "additional users", "more licenses")),
    ("satisfaction", 0.85, ("love it", "love this", "amazing", "fantastic", "works great", "so helpful",
                            "thank you so much", "best", "impressed")),
    ("feature_request", 0.8, ("feature", "would be great if", "can you add", "wish it", "it would help if",
                              "any plans to", "integrate with")),
    ("billing", 0.8, ("invoice", "charge", "payment", "billing", "subscription", "receipt", "refunded")),
    ("onboarding_help", 0.78, ("set up my profile", "get started", "onboarding", "claim my", "claim link",
                               "finish setup", "activate my", "verify my")),
)


class StubSupportClassifier:
    name = "stub"

    def classify(self, *, text: str, context: str = "") -> SupportClassification:
        t = (text or "").lower()
        for intent, conf, keys in _RULES:
            if any(k in t for k in keys):
                return SupportClassification(intent=intent, confidence=conf, rationale=f"matched {intent}")
        if "how do i" in t or "how can i" in t or "where is" in t or "?" in t:
            return SupportClassification(intent="how_to", confidence=0.65, rationale="how-to question")
        return SupportClassification(intent="other", confidence=0.4, rationale="no rule matched")
