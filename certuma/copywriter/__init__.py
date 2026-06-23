"""The COPYWRITER node (Phase 1 task P1.8) - the first Claude node.

Drafts per-physician email from an APPROVED template, renders the deterministic compliance tokens,
lints the result (certuma_core.linter), retries once, and returns a RenderedEmail the SENDER
consumes - or routes the lead to needs_review. A StubCopyProvider (deterministic, no LLM) backs the
tests; AnthropicCopyProvider is the real node (Sonnet for volume, Opus for high-value). Model IDs
and the structured-output mechanism are grounded in the Claude API reference.
"""
from .provider import CopyOutput, CopyProvider, TokenAudit
from .stub import StubCopyProvider
from .anthropic_provider import AnthropicCopyProvider
from .render import render
from .node import HAIKU, OPUS, SONNET, CopyResult, draft_email, select_model

__all__ = [
    "CopyOutput", "CopyProvider", "TokenAudit", "StubCopyProvider", "AnthropicCopyProvider",
    "render", "CopyResult", "draft_email", "select_model", "HAIKU", "SONNET", "OPUS",
]
