"""The COPYWRITER node (Phase 1 task P1.8).

draft_email: fetch the APPROVED template -> build SeedFacts -> pick the model tier -> provider.draft
-> render compliance tokens -> lint -> retry once -> return a RenderedEmail or route to needs_review.
The node never sends; the SENDER (P1.4) consumes the returned RenderedEmail. The model tier honors
the locked decision (Sonnet for volume, Opus for high-value = activation_priority high AND
practice_group_size >= 3); IDs verified against the Claude API reference.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma_core import campaigns, linter, urls
from certuma_core.learning import assign_variant
from certuma_core.copy_schema import SeedFacts, allowlist_sources
from certuma.config import Settings, get_settings
from certuma.db.models import Lead, Prospect, Template, WorkflowScore
from certuma.observability import METRICS, emit, get_logger
from certuma.sender import RenderedEmail

from .render import render

__all__ = ["HAIKU", "SONNET", "OPUS", "CopyResult", "select_model", "draft_email"]

_LOG = get_logger("certuma.copywriter")

HAIKU = "claude-haiku-4-5"      # reply classification (Phase 2)
SONNET = "claude-sonnet-4-6"    # volume first-touch drafting
OPUS = "claude-opus-4-8"        # high-value / template authoring / objection drafts


def select_model(priority: str, group_size: int) -> str:
    """Opus for high-value (priority=high AND practice_group_size>=3), else Sonnet (decision #3/#8)."""
    return OPUS if (priority == "high" and (group_size or 0) >= 3) else SONNET


@dataclass(frozen=True)
class CopyResult:
    ok: bool
    rendered: Optional[RenderedEmail] = None
    reason: str = ""
    violations: Tuple[str, ...] = ()
    model: str = ""


def _approved_template(session: Session, campaign: Optional[str]) -> Optional[Template]:
    tpl = session.execute(
        select(Template).where(Template.campaign == campaign, Template.is_approved.is_(True))
        .order_by(Template.version.desc()).limit(1)
    ).scalar()
    if tpl is None:  # fall back to a campaign-agnostic approved template
        tpl = session.execute(
            select(Template).where(Template.campaign.is_(None), Template.is_approved.is_(True))
            .order_by(Template.version.desc()).limit(1)
        ).scalar()
    return tpl


def _approved_variants(session: Session, campaign: Optional[str]) -> List[Template]:
    """All approved templates for the campaign (its A/B variants); falls back to the campaign-agnostic
    approved template when the campaign has none of its own."""
    rows = session.execute(
        select(Template).where(Template.campaign == campaign, Template.is_approved.is_(True))
        .order_by(Template.version, Template.id)
    ).scalars().all()
    if rows:
        return list(rows)
    return list(session.execute(
        select(Template).where(Template.campaign.is_(None), Template.is_approved.is_(True))
        .order_by(Template.version, Template.id)
    ).scalars().all())


def draft_email(
    session: Session,
    lead: Lead,
    *,
    provider,
    settings: Optional[Settings] = None,
    max_attempts: int = 2,
) -> CopyResult:
    settings = settings or get_settings()

    # A/B (P3.7): if a campaign has multiple approved variants, assign one STABLY per clinician
    # (npi), so a lead never switches variant and its outcome attributes to a single variant.
    variants = _approved_variants(session, lead.campaign)
    if not variants:
        METRICS.incr("copy_no_template")
        return CopyResult(ok=False, reason="no_approved_template")
    if len(variants) > 1:
        template = assign_variant(variants, key=lead.npi)
        variant_label = template.variant_label or f"v{template.version}"
    else:
        template = variants[0]
        variant_label = None  # single template: keep the provider's variant id (unchanged behavior)

    prospect = session.get(Prospect, lead.npi)
    preset = campaigns.CAMPAIGN_PRESETS.get(lead.campaign)
    pitch = preset.pitch_angle if preset else (getattr(prospect, "primary_specialty", "") or "")
    facts = SeedFacts(
        npi=lead.npi,
        first_name=getattr(prospect, "first_name", "") or "",
        last_name=getattr(prospect, "last_name", "") or "",
        display_name=getattr(prospect, "display_name", "") or "",
        credential=getattr(prospect, "credential", "") or "",
        specialty=getattr(prospect, "primary_specialty", "") or "",
        city=getattr(prospect, "practice_city", "") or "",
        state=getattr(prospect, "practice_state", "") or "",
        pitch_angle=pitch or "",
    )

    score = session.execute(
        select(WorkflowScore).where(WorkflowScore.npi == lead.npi)
        .order_by(WorkflowScore.scored_at.desc()).limit(1)
    ).scalar()
    model = select_model(score.activation_priority if score else "",
                         score.practice_group_size if score else 0)

    claim_url = lead.claim_url or ""
    domain = settings.cold_domain or "localhost"
    unsubscribe_url = urls.unsubscribe_url(domain, lead.npi)
    unsubscribe_mailto = urls.unsubscribe_mailto(domain)
    postal = settings.postal_address
    sender_identity = f"{settings.sender_from_name} {settings.sender_from_title}".strip()
    sources = allowlist_sources(facts, template_prose=template.body, sender_identity=sender_identity)

    last_violations: Tuple[str, ...] = ()
    for _ in range(max_attempts):
        copy = provider.draft(
            template_subject=template.subject, template_body=template.body, facts=facts, model=model
        )
        if variant_label:
            copy = replace(copy, variant_id=variant_label)  # tag the touch with its A/B variant
        rendered = render(copy, claim_url=claim_url, unsubscribe_url=unsubscribe_url,
                          unsubscribe_mailto=unsubscribe_mailto, postal_address=postal)
        result = linter.lint(
            subject=rendered.subject, body=rendered.body, plaintext=rendered.plaintext,
            allowlist_sources=sources, claim_url=claim_url,
            unsubscribe_url=unsubscribe_url, postal_address=postal,
        )
        if result.ok:
            METRICS.incr("copy_drafted", model=model)
            emit(_LOG, "copy_drafted", npi=lead.npi, model=model, variant=copy.variant_id)
            return CopyResult(ok=True, rendered=rendered, model=model)
        last_violations = result.violations

    METRICS.incr("copy_lint_failed")
    emit(_LOG, "copy_needs_review", npi=lead.npi, violations=list(last_violations))
    return CopyResult(ok=False, reason="lint_failed", violations=tuple(last_violations), model=model)
