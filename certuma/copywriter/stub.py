"""StubCopyProvider (Phase 1 task P1.8) - deterministic, no LLM.

Fills personalization tokens from SeedFacts and leaves the three compliance tokens literal.
Backs the tests and is a safe fallback. The COPYWRITER node + linter wrap it identically to the
real Anthropic provider, so the whole pipeline is testable without a network call.
"""
from __future__ import annotations

from certuma_core.copy_schema import SeedFacts

from .provider import CopyOutput, TokenAudit

__all__ = ["StubCopyProvider"]

_PERSONALIZATION = {
    "{first_name}": lambda f: f.first_name,
    "{last_name}": lambda f: f.last_name,
    "{display_name}": lambda f: f.display_name,
    "{credential}": lambda f: f.credential,
    "{pitch_angle}": lambda f: f.pitch_angle,
    "{city}": lambda f: f.city,
    "{state}": lambda f: f.state,
    "{specialty}": lambda f: f.specialty,
}


class StubCopyProvider:
    name = "stub"

    def draft(self, *, template_subject: str, template_body: str, facts: SeedFacts, model: str = "stub") -> CopyOutput:
        audit: list[TokenAudit] = []

        def fill(text: str) -> str:
            for token, getter in _PERSONALIZATION.items():
                if token in text:
                    text = text.replace(token, getter(facts))
                    audit.append(TokenAudit(token=token, source="SeedFacts"))
            return text

        subject = fill(template_subject or "")
        body = fill(template_body or "")
        # dedupe audit while preserving order
        seen, deduped = set(), []
        for a in audit:
            if a.token not in seen:
                seen.add(a.token)
                deduped.append(a)
        return CopyOutput(subject=subject, body=body, plaintext=body, variant_id="stub-v1",
                          merge_token_audit=tuple(deduped))
