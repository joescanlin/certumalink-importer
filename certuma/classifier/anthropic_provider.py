"""AnthropicReplyClassifier (Phase 2 task P2.2) - the real Haiku reply classifier.

Mirrors the copywriter's AnthropicCopyProvider: lazy SDK import, the Messages API structured-output
mechanism (output_config.format with a json_schema) to force the strict ClassificationResult shape,
and a refusal check. Model is the bare string claude-haiku-4-5 (no date suffix); reply
classification is a cheap, high-volume task, so Haiku is the right tier. A safety refusal
(stop_reason 'refusal') returns the conservative 'unknown' intent so the lead escalates to a human
rather than being mis-acted on.

NOTE: not exercised by the test suite (no API key/SDK in CI); verify the model id and the
output_config.format shape against the live Claude API reference at integration time.
"""
from __future__ import annotations

import json

from .provider import CLASSIFY_SCHEMA, INTENTS, ClassificationResult

__all__ = ["AnthropicReplyClassifier", "HAIKU", "SYSTEM_PROMPT"]

HAIKU = "claude-haiku-4-5"

# The default system prompt. Editable per-agent from the Agent Studio (stored in the agent table).
SYSTEM_PROMPT = (
    "You classify a single inbound email reply from a physician (or their staff) to a cold "
    "outreach message. Return ONLY the strict JSON shape requested. Choose exactly one intent from: "
    + ", ".join(INTENTS) + ". Guidance: 'unsubscribe' = an explicit opt-out / remove request; "
    "'not_interested' = a soft decline; 'objection' = a concern or pushback (price, legitimacy, "
    "HIPAA, how-did-you-get-my-email); 'question' = an answerable question; 'interested' = wants to "
    "proceed or claim; 'out_of_office'/'auto_reply' = an automated reply; 'wrong_person' = not the "
    "physician or no longer reachable; 'unknown' = you are not confident. When unsure, prefer the "
    "more conservative intent. confidence is your calibrated probability in [0,1]."
)


class AnthropicReplyClassifier:
    name = "anthropic"

    def __init__(self, api_key: str, *, client=None, model: str = HAIKU, system: str = ""):
        self._api_key = api_key
        self._client = client
        self._model = model
        self._system = system or SYSTEM_PROMPT

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: the SDK is only needed for the real provider
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def classify(self, *, text: str, context: str = "") -> ClassificationResult:
        client = self._get_client()
        user = (f"Outbound message we sent (context):\n{context}\n\n" if context else "") + \
               f"The physician's reply:\n{text}"
        response = client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=self._system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA}},
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return ClassificationResult(intent="unknown", confidence=0.0, rationale="model refusal")
        raw = next(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = json.loads(raw)
        intent = data["intent"] if data.get("intent") in INTENTS else "unknown"
        return ClassificationResult(intent=intent, confidence=float(data.get("confidence", 0.0)),
                                    rationale=data.get("rationale", ""))
