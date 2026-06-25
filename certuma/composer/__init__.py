"""Template composer (Studio AI compose).

Authors outbound + follow-up email TEMPLATES from a brief, a message type and a chosen model, then
inserts them as approved-able A/B variants tagged with the authoring model - so the sales lead can
prompt the AI to write copy, assign it across a split of variants, and let the existing A/B engine
pick a winner. Real Anthropic node when CERTUMA_ANTHROPIC_API_KEY is set, deterministic stub
otherwise. node.py needs SQLAlchemy, so it is imported lazily to keep the pure provider/stub usable
(and unit-testable) without a DB stack.
"""
from .provider import (BANNED_CLAIMS, COMPOSE_SCHEMA, DEFAULT_MODEL, MESSAGE_TYPE_KEYS,
                       MESSAGE_TYPE_LABELS, MESSAGE_TYPES, MODEL_IDS, MODELS, REQUIRED_TOKENS,
                       ComposeOutput, ComposeProvider, ComposeRequest)
from .stub import StubComposeProvider
from .anthropic_provider import AnthropicComposeProvider, ComposeRefused

_NODE_NAMES = ("ComposeResult", "build_provider", "compose_template", "insert_template",
               "next_variant_label")

__all__ = [
    "MESSAGE_TYPES", "MESSAGE_TYPE_KEYS", "MESSAGE_TYPE_LABELS", "MODELS", "MODEL_IDS",
    "DEFAULT_MODEL", "REQUIRED_TOKENS", "BANNED_CLAIMS", "COMPOSE_SCHEMA", "ComposeRequest",
    "ComposeOutput", "ComposeProvider", "StubComposeProvider", "AnthropicComposeProvider",
    "ComposeRefused", *_NODE_NAMES,
]


def __getattr__(name):
    if name in _NODE_NAMES:
        from . import node
        return getattr(node, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
