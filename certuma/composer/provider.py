"""Template-composer interfaces + value types (Studio AI compose).

The composer AUTHORS outbound/follow-up template copy (subject + body) from a brief, a message type
and a chosen model - distinct from the copywriter, which fills an already-approved template per lead.
The authored body must keep the three literal compliance tokens so it lints clean and can be
approved into the A/B set. Pure module: no SQLAlchemy, no SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

__all__ = [
    "MESSAGE_TYPES", "MESSAGE_TYPE_KEYS", "MESSAGE_TYPE_LABELS", "MODELS", "MODEL_IDS",
    "DEFAULT_MODEL", "REQUIRED_TOKENS", "BANNED_CLAIMS", "ComposeRequest", "ComposeOutput",
    "ComposeProvider", "COMPOSE_SCHEMA",
]

# the message types the studio can author (key, label)
MESSAGE_TYPES = (
    ("first_touch", "First touch"),
    ("follow_up_1", "Follow-up 1"),
    ("follow_up_2", "Follow-up 2"),
    ("objection_reply", "Objection reply"),
    ("re_engage", "Re-engage"),
)
MESSAGE_TYPE_KEYS = tuple(k for k, _ in MESSAGE_TYPES)
MESSAGE_TYPE_LABELS = {k: v for k, v in MESSAGE_TYPES}

# the model dropdown (label, id) - the current Claude family
MODELS = (
    ("Opus 4.8", "claude-opus-4-8"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Haiku 4.5", "claude-haiku-4-5"),
    ("Fable 5", "claude-fable-5"),
)
MODEL_IDS = tuple(m for _, m in MODELS)
DEFAULT_MODEL = "claude-opus-4-8"

# the literal compliance tokens an authored body MUST keep (rendered per-lead later)
REQUIRED_TOKENS = ("{claim_url}", "{unsubscribe_url}", "{postal_address}")
# claims the copy may never make (mirrors certuma.templates.approval)
BANNED_CLAIMS = ("board-certified", "board certified", "verified", "credentialed", "endorsed",
                 "accredited")


@dataclass(frozen=True)
class ComposeRequest:
    message_type: str
    brief: str = ""
    campaign: Optional[str] = None
    specialty: str = ""
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class ComposeOutput:
    subject: str
    body: str


class ComposeProvider(Protocol):
    name: str

    def compose(self, req: ComposeRequest) -> ComposeOutput:
        ...


COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}
