"""Customer-support agents (Phase 4 / support).

A second agentic loop that runs ALONGSIDE the sales agents: it classifies inbound onboarding/support
messages from (often activated) physicians, answers routine ones and escalates the rest, and turns
each interaction into a SALES signal in the shared clinician_signal knowledge graph - expansion
questions become upsell leads, raves become referral leads, complaints/bugs become churn signals.
Those signals flow straight into the sales scoring + recommended-actions that already read the graph,
so support work compounds into sales intelligence. Stub classifier backs the tests; AnthropicSupport
Classifier is the real Haiku node.
"""
from .provider import (ADVOCATE, CHURN_RISK, EFFECTS, EXPANSION_INTENT, SUPPORT_INTENTS,
                       SUPPORT_SIGNALS, SupportClassification, SupportClassifier)
from .stub import StubSupportClassifier
from .anthropic_provider import AnthropicSupportClassifier

# node.py needs SQLAlchemy; import it lazily so the pure classifier/provider can be used (and unit
# tested) without a DB stack present, mirroring how the rest of certuma keeps its no-DB import path.
_NODE_NAMES = ("SupportOutcome", "SupportSummary", "emit_sales_signal", "handle_ticket", "run_support",
               "reclassify", "override_intent", "set_status", "bulk_set_status")

__all__ = [
    "SUPPORT_INTENTS", "SUPPORT_SIGNALS", "EXPANSION_INTENT", "ADVOCATE", "CHURN_RISK", "EFFECTS",
    "SupportClassification", "SupportClassifier", "StubSupportClassifier", "AnthropicSupportClassifier",
    *_NODE_NAMES,
]


def __getattr__(name):
    if name in _NODE_NAMES:
        from . import node
        return getattr(node, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
