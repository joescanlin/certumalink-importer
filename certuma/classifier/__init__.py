"""The reply classifier (Phase 2 task P2.2) - the second Claude node (Haiku).

Labels an inbound reply's intent and drives a deterministic ledger transition from that label; the
model only labels, never moves a lead itself, and can never reach physician_activated. A
StubReplyClassifier (deterministic keyword rules) backs the tests; AnthropicReplyClassifier is the
real Haiku node. The model id and structured-output mechanism are grounded in the Claude API
reference (same pattern as the copywriter).
"""
from .provider import CLASSIFY_SCHEMA, INTENTS, ClassificationResult, ReplyClassifier
from .stub import StubReplyClassifier
from .anthropic_provider import HAIKU, AnthropicReplyClassifier
from .node import ClassifyOutcome, RESCHEDULE_DAYS, classify_reply

__all__ = [
    "INTENTS", "CLASSIFY_SCHEMA", "ClassificationResult", "ReplyClassifier",
    "StubReplyClassifier", "AnthropicReplyClassifier", "HAIKU",
    "ClassifyOutcome", "classify_reply", "RESCHEDULE_DAYS",
]
