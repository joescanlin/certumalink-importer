"""CAN-SPAM completeness check (Phase 1 task P1.3). Read-only.

A *transient config* gate: if the system cannot currently send a compliant email (no postal
address, no sender identity, or the campaign's approved template is missing the unsubscribe /
postal tokens), the Gate HOLDs `can_spam_incomplete` so the item is re-queued when the config is
fixed - NOT a permanent suppression-class BLOCK. Returns a reason string or None.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma.config import Settings
from certuma.db.models import Template

__all__ = ["can_spam_incomplete"]

_REQUIRED_TEMPLATE_TOKENS = ("{unsubscribe_url}", "{postal_address}", "{claim_url}")


def can_spam_incomplete(session: Session, campaign: Optional[str], settings: Settings) -> Optional[str]:
    if not settings.postal_address.strip():
        return "no_postal_address"
    if not settings.sender_from_email.strip():
        return "no_sender_identity"
    # the campaign must have an approved template that carries the compliance tokens
    body = session.execute(
        select(Template.body)
        .where(Template.campaign == campaign, Template.is_approved.is_(True))
        .order_by(Template.version.desc())
        .limit(1)
    ).scalar()
    if body is None:
        # fall back to a campaign-agnostic approved template (campaign IS NULL)
        body = session.execute(
            select(Template.body)
            .where(Template.campaign.is_(None), Template.is_approved.is_(True))
            .order_by(Template.version.desc())
            .limit(1)
        ).scalar()
    if body is None:
        return "no_approved_template"
    missing = [tok for tok in _REQUIRED_TEMPLATE_TOKENS if tok not in body]
    if missing:
        return f"template_missing_tokens:{','.join(missing)}"
    return None
