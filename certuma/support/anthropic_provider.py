"""AnthropicSupportClassifier (Phase 4 / support) - the real Haiku support-intent classifier.

Mirrors the reply classifier: lazy SDK import, structured output (output_config.format json_schema)
to force the strict shape, and a refusal -> conservative 'other' so an uncertain ticket escalates to
a human. Model is claude-haiku-4-5 (cheap, high-volume). Verify the id + output mechanism against the
Claude API reference at integration.
"""
from __future__ import annotations

import json

from .provider import SUPPORT_INTENTS, SUPPORT_SCHEMA, SupportClassification

__all__ = ["AnthropicSupportClassifier", "HAIKU", "SYSTEM_PROMPT"]

HAIKU = "claude-haiku-4-5"

SYSTEM_PROMPT = (
    "You classify a single inbound customer-support message from a physician (or their staff) who is "
    "onboarding to or using Certumalink. Return ONLY the strict JSON shape requested; choose exactly "
    "one intent from: " + ", ".join(SUPPORT_INTENTS) + ". Guidance: 'expansion_interest' = wants more "
    "seats/locations/providers; 'feature_request' = wants a capability we may not have; 'satisfaction' "
    "= a positive/thankful message; 'complaint' = unhappy / wants to cancel; 'bug_report' = something "
    "is broken; 'billing' = an invoice/payment question; 'onboarding_help' = setup/claim help; "
    "'how_to' = a general usage question; 'other' = anything else. When unsure, prefer 'other'."
)


class AnthropicSupportClassifier:
    name = "anthropic"

    def __init__(self, api_key: str, *, client=None, model: str = HAIKU, system: str = ""):
        self._api_key = api_key
        self._client = client
        self._model = model
        self._system = system or SYSTEM_PROMPT

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def classify(self, *, text: str, context: str = "") -> SupportClassification:
        client = self._get_client()
        user = (f"Subject: {context}\n\n" if context else "") + f"Message:\n{text}"
        response = client.messages.create(
            model=self._model, max_tokens=1024, system=self._system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SUPPORT_SCHEMA}})
        if getattr(response, "stop_reason", None) == "refusal":
            return SupportClassification(intent="other", confidence=0.0, rationale="model refusal")
        raw = next(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = json.loads(raw)
        intent = data["intent"] if data.get("intent") in SUPPORT_INTENTS else "other"
        return SupportClassification(intent=intent, confidence=float(data.get("confidence", 0.0)),
                                     rationale=data.get("rationale", ""))
