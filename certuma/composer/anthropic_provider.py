"""AnthropicComposeProvider (Studio AI compose) - the real template-authoring node.

Mirrors the copywriter/support providers: lazy SDK import, structured output (output_config.format
json_schema) to force the strict {subject, body} shape, and the model taken from the request so the
studio dropdown picks the tier. The system prompt hard-requires the three literal compliance tokens
and forbids the banned claims; the node still lints the result, so a non-compliant draft surfaces in
the studio rather than being silently approved. A refusal raises ComposeRefused. Verify the model
ids + output mechanism against the Claude API reference at integration.
"""
from __future__ import annotations

import json

from .provider import (BANNED_CLAIMS, COMPOSE_SCHEMA, DEFAULT_MODEL, MESSAGE_TYPE_LABELS,
                       ComposeOutput, ComposeRequest)

__all__ = ["AnthropicComposeProvider", "ComposeRefused", "SYSTEM_PROMPT"]

SYSTEM_PROMPT = (
    "You are Certuma's outreach copywriter, authoring a single cold-outreach email TEMPLATE to a "
    "physician (or their staff) for the Certumalink provider-profile product. Rules, all mandatory: "
    "(1) Return ONLY the strict JSON shape requested: a subject and a body. (2) The body MUST contain "
    "these three tokens EXACTLY as written, unaltered: {claim_url}, {unsubscribe_url}, {postal_address} "
    "- {claim_url} where the physician reviews/claims their profile, {unsubscribe_url} and "
    "{postal_address} in the footer. (3) You MAY use the {last_name} personalization token; use no "
    "other curly-brace tokens. (4) NEVER claim the physician is verified, board-certified, "
    "credentialed, endorsed, or accredited - this is a draft profile we prepared, not an endorsement. "
    "(5) Keep it concise, warm and professional."
)


class ComposeRefused(RuntimeError):
    """The model declined to author the template (stop_reason='refusal')."""


class AnthropicComposeProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, client=None, model: str = DEFAULT_MODEL, system: str = ""):
        self._api_key = api_key
        self._client = client
        self._model = model
        self._system = system or SYSTEM_PROMPT

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: only the real provider needs the SDK
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def compose(self, req: ComposeRequest) -> ComposeOutput:
        client = self._get_client()
        type_label = MESSAGE_TYPE_LABELS.get(req.message_type, req.message_type)
        user = (
            f"Message type: {type_label}\n"
            f"Campaign: {req.campaign or 'general'}\n"
            f"Specialty focus: {req.specialty or 'any specialty'}\n\n"
            f"Brief from the sales lead:\n{req.brief or '(no extra brief - use your best judgment)'}\n\n"
            "Author the template now. Remember the three required compliance tokens and the banned "
            f"claims: {', '.join(BANNED_CLAIMS)}."
        )
        response = client.messages.create(
            model=req.model or self._model, max_tokens=1500, system=self._system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": COMPOSE_SCHEMA}})
        if getattr(response, "stop_reason", None) == "refusal":
            raise ComposeRefused("model declined to author the template")
        raw = next(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = json.loads(raw)
        return ComposeOutput(subject=data["subject"], body=data["body"])
