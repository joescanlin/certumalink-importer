"""Template-composer node (Studio AI compose).

compose_template authors a draft (subject + body) for a message type and lints it with the same
template-level compliance check the approval flow uses, so the studio can show problems before
anyone inserts it. insert_template writes the authored copy as a new, versioned A/B variant of a
campaign (tagged with the authoring model + message type), optionally approving it in the same step
(which re-lints and refuses a non-compliant body). build_provider chooses the real Anthropic provider
when an API key is configured, else the deterministic stub - so the studio works with or without a key.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from certuma.config import Settings, get_settings
from certuma.db.models import Template
from certuma.observability import METRICS, emit, get_logger
from certuma.templates import approve_template, lint_template

from .provider import DEFAULT_MODEL, MESSAGE_TYPE_KEYS, ComposeRequest
from .stub import StubComposeProvider

__all__ = ["ComposeResult", "build_provider", "compose_template", "insert_template",
           "next_variant_label"]

_LOG = get_logger("certuma.composer")
_LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class ComposeResult:
    subject: str
    body: str
    model: str
    message_type: str
    ok: bool
    problems: Tuple[str, ...] = ()


def build_provider(settings: Optional[Settings] = None, *, model: str = DEFAULT_MODEL):
    """The real Anthropic composer when a key is set, else the deterministic stub."""
    settings = settings or get_settings()
    key = getattr(settings, "anthropic_api_key", "")
    if key:
        from .anthropic_provider import AnthropicComposeProvider
        return AnthropicComposeProvider(key, model=model)
    return StubComposeProvider()


def compose_template(session: Session, *, request: ComposeRequest, provider=None,
                     settings: Optional[Settings] = None) -> ComposeResult:
    """Author one template draft and lint it. Read-only (writes nothing). Caller may then insert."""
    provider = provider or build_provider(settings, model=request.model)
    out = provider.compose(request)
    problems = lint_template(Template(subject=out.subject, body=out.body))  # transient, not added
    METRICS.incr("template_composed", message_type=request.message_type, ok=str(not problems))
    emit(_LOG, "template_composed", message_type=request.message_type, model=request.model,
         ok=not problems)
    return ComposeResult(subject=out.subject, body=out.body, model=request.model,
                         message_type=request.message_type, ok=not problems,
                         problems=tuple(problems))


def next_variant_label(session: Session, campaign: Optional[str],
                       message_type: Optional[str] = None) -> str:
    """The next free A/B label (A, B, C, ...) for a campaign + message-type's templates. Scoped by
    message_type so variant 'A' can span the cadence (a first-touch A and a follow-up A both belong
    to treatment A). Raises ValueError rather than minting a duplicate label once the alphabet is
    exhausted."""
    cond = _campaign_eq(campaign)
    if message_type is not None:
        cond = and_(cond, Template.message_type == message_type)
    used = set(session.execute(select(Template.variant_label).where(cond)).scalars().all())
    for ch in _LABELS:
        if ch not in used:
            return ch
    raise ValueError("too many variants for this campaign and message type")


def _campaign_eq(campaign: Optional[str]):
    return Template.campaign.is_(None) if campaign in (None, "") else Template.campaign == campaign


def insert_template(session: Session, *, campaign: Optional[str], subject: str, body: str,
                    message_type: str, model: str, variant_label: str = "",
                    created_by: str = "console", approve: bool = False) -> Template:
    """Insert authored copy as a new versioned A/B variant. Caller commits."""
    if message_type not in MESSAGE_TYPE_KEYS:
        raise ValueError(f"unknown message_type {message_type!r}")
    if not subject.strip() or not body.strip():
        raise ValueError("subject and body are required")
    campaign = campaign or None
    max_version = session.execute(
        select(func.max(Template.version)).where(_campaign_eq(campaign))
    ).scalar() or 0
    label = (variant_label or next_variant_label(session, campaign, message_type)).strip()
    tpl = Template(campaign=campaign, version=max_version + 1, subject=subject.strip(), body=body,
                   message_type=message_type, model=model, source="ai", variant_label=label,
                   created_by=created_by, is_approved=False)
    session.add(tpl)
    session.flush()
    if approve:
        approve_template(session, tpl.id, created_by)  # re-lints; raises ValueError if non-compliant
    METRICS.incr("template_inserted", message_type=message_type, approved=str(bool(approve)))
    emit(_LOG, "template_inserted", template_id=tpl.id, campaign=campaign, variant=label,
         message_type=message_type, model=model, approved=bool(approve))
    return tpl
