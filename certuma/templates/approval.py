"""Template approval flow (Phase 1 task P1.7).

A campaign template must be approved by a human before the COPYWRITER will draft from it and
before the Gate's CAN-SPAM check passes. approve_template flips is_approved, records approved_by,
and writes an append-only audit_log row. lint_template runs the deterministic compliance checks on
the raw template body so the studio can preview problems before approval (the required compliance
tokens must be PRESENT as tokens in an unapproved template; the rendered values are linted later).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from certuma.db.models import AuditLog, Template

__all__ = ["TemplateNotFound", "approve_template", "lint_template"]

_REQUIRED_TOKENS = ("{unsubscribe_url}", "{postal_address}", "{claim_url}")
_BANNED = ("board-certified", "board certified", "verified", "credentialed", "endorsed", "accredited")


class TemplateNotFound(Exception):
    pass


def lint_template(template: Template) -> list:
    """Return a list of compliance problems with a template body (empty == ok to approve)."""
    body = template.body or ""
    problems = []
    for token in _REQUIRED_TOKENS:
        if token not in body:
            problems.append(f"missing required token {token}")
    low = (template.subject or "").lower() + "\n" + body.lower()
    for claim in _BANNED:
        if claim in low:
            problems.append(f"banned claim: {claim!r}")
    return problems


def approve_template(session: Session, template_id: int, approved_by: str) -> Template:
    """Approve a template after it passes lint. Raises TemplateNotFound / ValueError(problems)."""
    template = session.get(Template, template_id)
    if template is None:
        raise TemplateNotFound(str(template_id))
    problems = lint_template(template)
    if problems:
        raise ValueError(f"template not compliant: {problems}")
    template.is_approved = True
    template.approved_by = approved_by
    session.add(AuditLog(
        entity="template", entity_id=str(template_id), action="approve",
        actor=approved_by, new_value={"is_approved": True},
    ))
    session.flush()
    return template
