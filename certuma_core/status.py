"""Activation status state machine.

Replaces the monolith's flat VALID_ACTIVATION_STATUSES *set* (:37-47) with an explicit
ALLOWED_TRANSITIONS graph (Phase-0 task B2). The 9 legacy states are extended with 5
agentic/intermediate states. normalize_status() reproduces the monolith's
_normalize_activation_status (:1511-1515) exactly.
"""
from __future__ import annotations

__all__ = [
    "DEFAULT_STATE",
    "STATES",
    "LEGACY_STATUS_MAP",
    "TERMINAL_STATES",
    "QUEUE_EXCLUDED_STATES",
    "ALLOWED_TRANSITIONS",
    "ACTIVATION_ONLY_ACTORS",
    "IllegalTransition",
    "normalize_status",
    "is_legal_transition",
    "assert_transition",
]

DEFAULT_STATE = "not_contacted"

# monolith LEGACY_ACTIVATION_STATUS_MAP (:32-36) — lifted verbatim.
LEGACY_STATUS_MAP = {
    "draft_profile_created": "not_contacted",
    "rox_contacted": "email_sent",
    "activated": "physician_activated",
}

# 9 legacy states + 5 agentic/intermediate (enriching, sendable, awaiting_reply, replied, exhausted).
STATES = frozenset(
    {
        "not_contacted",
        "queued_today",
        "enriching",
        "sendable",
        "email_sent",
        "awaiting_reply",
        "replied",
        "interested",
        "called_no_answer",  # legacy (voice; carried for migration parity)
        "voicemail_left",    # legacy (voice; carried for migration parity)
        "physician_activated",
        "do_not_contact",
        "needs_review",
        "exhausted",
    }
)

TERMINAL_STATES = frozenset({"physician_activated", "do_not_contact", "exhausted"})

# Queue eligibility exclusion. Superset of the monolith's QUEUE_EXCLUDED_STATUSES (:147)
# {physician_activated, do_not_contact, needs_review} plus the new terminal `exhausted`.
QUEUE_EXCLUDED_STATES = TERMINAL_STATES | {"needs_review"}

# Only these actors may set physician_activated — protects the sole conversion metric
# (decision #6: claim_url click). Enforced by the ledger-writer, asserted here for reuse.
ACTIVATION_ONLY_ACTORS = frozenset({"poller", "activation_webhook"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_contacted": frozenset({"queued_today", "enriching", "do_not_contact", "needs_review"}),
    "queued_today": frozenset({"enriching", "sendable", "do_not_contact", "needs_review"}),
    "enriching": frozenset({"sendable", "needs_review", "do_not_contact", "exhausted"}),
    "sendable": frozenset({"email_sent", "enriching", "needs_review", "do_not_contact"}),
    # email_sent / awaiting_reply -> interested are the claim-click activation edges (Phase 1):
    # a claim_url click is an activation signal independent of any reply, so the poller must be
    # able to reach `interested` (the only state from which `physician_activated` is legal) from a
    # real send without waiting on the Phase 2 reply classifier. Both are driven by actor='poller'.
    "email_sent": frozenset(
        {"awaiting_reply", "replied", "interested", "enriching", "exhausted", "do_not_contact", "needs_review"}
    ),
    "awaiting_reply": frozenset(
        {"replied", "interested", "email_sent", "enriching", "exhausted", "needs_review", "do_not_contact"}
    ),
    "replied": frozenset({"interested", "needs_review", "do_not_contact", "awaiting_reply", "exhausted"}),
    "interested": frozenset(
        {"physician_activated", "awaiting_reply", "email_sent", "do_not_contact", "needs_review", "exhausted"}
    ),
    "needs_review": frozenset({"sendable", "queued_today", "enriching", "do_not_contact", "exhausted"}),
    "called_no_answer": frozenset(
        {"voicemail_left", "email_sent", "awaiting_reply", "do_not_contact", "needs_review"}
    ),  # legacy
    "voicemail_left": frozenset({"email_sent", "awaiting_reply", "do_not_contact", "needs_review"}),  # legacy
    "physician_activated": frozenset(),  # TERMINAL success
    "do_not_contact": frozenset(),       # TERMINAL suppression
    "exhausted": frozenset(),            # TERMINAL non-success
}


class IllegalTransition(Exception):
    """Raised when a status transition is not permitted by ALLOWED_TRANSITIONS."""


def normalize_status(status: object) -> str:
    """Trim + apply legacy map + default. (monolith _normalize_activation_status, :1511-1515)"""
    cleaned = str(status or "").strip()
    if not cleaned:
        return DEFAULT_STATE
    return LEGACY_STATUS_MAP.get(cleaned, cleaned)


def is_legal_transition(old: str, new: str) -> bool:
    return new in ALLOWED_TRANSITIONS.get(old, frozenset())


def assert_transition(old: str, new: str) -> None:
    if old in TERMINAL_STATES:
        raise IllegalTransition(f"{old} is terminal; cannot move to {new}")
    if not is_legal_transition(old, new):
        raise IllegalTransition(f"{old} -> {new} not allowed")
