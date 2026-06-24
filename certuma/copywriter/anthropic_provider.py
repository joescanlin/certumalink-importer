"""AnthropicCopyProvider (Phase 1 task P1.8) - the real Claude copy node.

Uses the Messages API structured-output mechanism (output_config.format with a json_schema) to
force the strict CopyOutput shape, per the Claude API reference. Model IDs are the bare strings
claude-sonnet-4-6 / claude-opus-4-8 (no date suffixes). The anthropic SDK is imported lazily so the
rest of the system (and the tests, which use StubCopyProvider) never require it. The client is
injectable for testing. A safety refusal (stop_reason 'refusal') raises CopyRefused so the node
routes the lead to needs_review rather than sending.

NOTE: not exercised by the test suite (no API key/SDK in CI); verify model IDs and the
output_config.format shape against the live Claude API reference at integration time.
"""
from __future__ import annotations

import json
from typing import Optional

from certuma_core.copy_schema import COPY_OUTPUT_SCHEMA, SeedFacts

from .provider import CopyOutput, TokenAudit

__all__ = ["AnthropicCopyProvider", "CopyRefused", "SYSTEM_PROMPT"]

# The default system prompt. Editable per-agent from the Agent Studio (stored in the agent table);
# this is the canonical fallback and the seed value.
SYSTEM_PROMPT = (
    "You are Certuma, drafting a single cold outreach email to a physician from an APPROVED "
    "template. Rules: (1) Fill ONLY the personalization tokens using the provided facts; use no "
    "fact that is not provided. (2) Leave the literal tokens {claim_url}, {unsubscribe_url}, and "
    "{postal_address} EXACTLY as-is - never invent or alter them. (3) Never claim the physician is "
    "verified, board-certified, credentialed, endorsed, or accredited; this is a draft profile we "
    "prepared, not an endorsement. (4) Keep it concise and professional. Return the strict JSON "
    "shape requested."
)


class CopyRefused(RuntimeError):
    """The model declined to draft (stop_reason='refusal')."""


class AnthropicCopyProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, client=None, system: str = ""):
        self._api_key = api_key
        self._client = client
        self._system = system or SYSTEM_PROMPT

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: the SDK is only needed for the real provider
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def draft(self, *, template_subject: str, template_body: str, facts: SeedFacts, model: str) -> CopyOutput:
        client = self._get_client()
        user = (
            "Approved template:\n"
            f"SUBJECT: {template_subject}\n"
            f"BODY:\n{template_body}\n\n"
            "Facts (the only facts you may use):\n"
            + json.dumps({
                "first_name": facts.first_name, "last_name": facts.last_name,
                "display_name": facts.display_name, "credential": facts.credential,
                "specialty": facts.specialty, "city": facts.city, "state": facts.state,
                "pitch_angle": facts.pitch_angle,
            })
        )
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=self._system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": COPY_OUTPUT_SCHEMA}},
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise CopyRefused("copy model refused to draft")
        text = next(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = json.loads(text)
        return CopyOutput(
            subject=data["subject"], body=data["body"], plaintext=data["plaintext"],
            variant_id=data.get("variant_id", "v1"),
            merge_token_audit=tuple(
                TokenAudit(token=a["token"], source=a["source"]) for a in data.get("merge_token_audit", [])
            ),
        )
