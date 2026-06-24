"""The enrichment node (Phase 2 task P2.5).

enrich_lead runs the waterfall for one lead; run_enrichment is the batch entry (tick step 0). A lead
with no deliverable contact is discovered + verified; the best VALID candidate (personal preferred
over role) becomes a Contact and the lead advances not_contacted/queued -> enriching -> sendable.
Nothing valid -> needs_review. A suppressed prospect is skipped before any provider call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma import gate, monitor
from certuma.config import Settings, get_settings
from certuma.db.models import Contact, Lead, Prospect
from certuma.observability import METRICS, emit, get_logger

from .provider import VALID

__all__ = ["EnrichOutcome", "EnrichSummary", "enrich_lead", "run_enrichment", "ENRICHABLE_STATES"]

_LOG = get_logger("certuma.enrichment")

ENRICHABLE_STATES = ("not_contacted", "queued_today", "enriching")
# how many candidates to verify, by activation priority (budget) - provisional
_BUDGET = {"high": 5, "medium": 3, "low": 2}
_DEFAULT_BUDGET = 3


@dataclass(frozen=True)
class EnrichOutcome:
    found: bool
    email: Optional[str] = None
    transitioned_to: Optional[str] = None
    reason: str = ""


@dataclass
class EnrichSummary:
    attempted: int = 0
    enriched: int = 0
    no_contact: int = 0
    skipped: int = 0
    enriched_npis: List[str] = field(default_factory=list)


def _has_valid_contact(session: Session, npi: str) -> bool:
    return session.execute(
        select(Contact.id).where(Contact.npi == npi, Contact.email_status == VALID).limit(1)
    ).first() is not None


def enrich_lead(
    session: Session,
    lead: Lead,
    *,
    discovery,
    verify,
    settings: Optional[Settings] = None,
    budget: int = _DEFAULT_BUDGET,
    when: Optional[datetime] = None,
) -> EnrichOutcome:
    """Find + verify a deliverable contact for one lead (already in `enriching`). Caller commits."""
    settings = settings or get_settings()
    when = when or datetime.now(timezone.utc)

    if gate.is_suppressed(session, npi=lead.npi):
        return EnrichOutcome(found=False, reason="suppressed")

    if _has_valid_contact(session, lead.npi):  # already deliverable (e.g. a prior enrich / hand-seed)
        moved = monitor.try_transition(session, lead, "sendable", actor="enricher", reason_code="existing_contact")
        return EnrichOutcome(found=True, transitioned_to=("sendable" if moved else None), reason="existing_contact")

    prospect = session.get(Prospect, lead.npi)
    candidates = discovery.discover(
        first_name=getattr(prospect, "first_name", "") or "",
        last_name=getattr(prospect, "last_name", "") or "",
        display_name=getattr(prospect, "display_name", "") or "",
        npi=lead.npi,
    )[:budget]

    valid_personal = None
    valid_role = None
    for c in candidates:
        if verify.verify(c.email).status != VALID:
            continue
        if not c.is_role and valid_personal is None:
            valid_personal = c
        elif c.is_role and valid_role is None:
            valid_role = c
    chosen = valid_personal or valid_role

    if chosen is None:
        moved = monitor.try_transition(session, lead, "needs_review", actor="enricher",
                                       reason_code="no_valid_contact")
        METRICS.incr("enrich_no_contact")
        return EnrichOutcome(found=False, transitioned_to=("needs_review" if moved else None),
                             reason="no_valid_contact")

    session.add(Contact(npi=lead.npi, email=chosen.email, email_status=VALID, verifier=verify.name,
                        verified_at=when, is_role_address=chosen.is_role, discovery_source=chosen.source))
    lead.needs_reenrich = False
    moved = monitor.try_transition(session, lead, "sendable", actor="enricher", reason_code="enriched")
    session.flush()
    METRICS.incr("enrich_found")
    emit(_LOG, "enrich_found", lead_id=lead.id, npi=lead.npi, email=chosen.email, role=chosen.is_role)
    return EnrichOutcome(found=True, email=chosen.email, transitioned_to=("sendable" if moved else None))


def run_enrichment(
    session: Session,
    *,
    discovery,
    verify,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
    limit: int = 200,
) -> EnrichSummary:
    """Enrich every pre-send lead lacking a deliverable contact. Caller commits."""
    settings = settings or get_settings()
    when = when or datetime.now(timezone.utc)
    leads = session.execute(
        select(Lead).where(Lead.activation_status.in_(ENRICHABLE_STATES)).order_by(Lead.id).limit(limit)
    ).scalars().all()

    summary = EnrichSummary()
    for lead in leads:
        summary.attempted += 1
        if lead.activation_status != "enriching":
            if not monitor.try_transition(session, lead, "enriching", actor="enricher",
                                          reason_code="enrich_start"):
                summary.skipped += 1
                continue
        outcome = enrich_lead(session, lead, discovery=discovery, verify=verify, settings=settings, when=when)
        if outcome.found:
            summary.enriched += 1
            summary.enriched_npis.append(lead.npi)
        elif outcome.reason == "suppressed":
            # a suppressed prospect should not sit in the pipeline; stop it
            monitor.try_transition(session, lead, "do_not_contact", actor="enricher", reason_code="suppressed")
            summary.skipped += 1
        else:
            summary.no_contact += 1
    session.flush()
    METRICS.incr("enrich_run")
    emit(_LOG, "enrich_run", attempted=summary.attempted, enriched=summary.enriched,
         no_contact=summary.no_contact)
    return summary
