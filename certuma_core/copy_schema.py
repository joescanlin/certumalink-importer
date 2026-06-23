"""Copywriter structured-output schema + the fact allow-list (Phase 1 task P1.6).

SeedFacts is the ONLY fact source the copywriter may use (defense against hallucinated
practices/affiliations/credentials, given uncredentialed self-reported data). allowlist_sources
returns the literal strings the linter traces every multi-word proper noun back to.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["COPY_OUTPUT_SCHEMA", "SeedFacts", "allowlist_sources", "BANNED_CLAIMS"]

# the copywriter is forced to return exactly this shape
COPY_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "body", "plaintext", "variant_id", "merge_token_audit"],
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "plaintext": {"type": "string"},
        "variant_id": {"type": "string"},
        "merge_token_audit": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["token", "source"],
                "properties": {"token": {"type": "string"}, "source": {"type": "string"}},
            },
        },
    },
}

# claims forbidden because the data is provider-self-reported and uncredentialed
BANNED_CLAIMS = (
    "verified", "board-certified", "board certified", "credentialed", "credential-verified",
    "endorsed", "certified by", "accredited", "vetted", "background-checked", "licensed by us",
)


@dataclass(frozen=True)
class SeedFacts:
    npi: str
    first_name: str
    last_name: str
    display_name: str
    credential: str
    specialty: str
    city: str
    state: str
    pitch_angle: str


def allowlist_sources(
    facts: SeedFacts,
    *,
    template_prose: str = "",
    sender_identity: str = "",
) -> list[str]:
    """Every literal string the email may legitimately contain a proper noun from."""
    return [
        facts.first_name, facts.last_name, facts.display_name, facts.credential,
        facts.specialty, facts.city, facts.state, facts.pitch_angle,
        template_prose, sender_identity,
    ]
