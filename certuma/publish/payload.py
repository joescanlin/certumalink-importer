"""Publish request-payload builders (Phase 0 task B13).

Reproduces the documented request shape (docs/profile-seeding-api.md), built from DB entities
instead of CSV. Values are serialized as strings to match the documented wire contract that a
backend will implement. Duck-typed: reads attributes off any object with the right names, so
tests can pass simple stubs without the ORM. No DB import here (keeps this layer test-light).
"""
from __future__ import annotations

from typing import Iterable, Sequence

__all__ = ["PROFILE_FIELDS", "profile_payload_row", "build_payload"]

# the 26 per-profile fields, in documented order
PROFILE_FIELDS = [
    "npi", "profile_url", "profile_slug", "claim_url", "display_name", "first_name", "last_name",
    "credential", "specialty", "taxonomy_code", "city", "state", "practice_zip", "practice_phone",
    "source", "source_fetched_at", "campaign", "activation_status", "activation_priority",
    "activation_score", "priority_reason", "profile_completeness_score", "missing_profile_fields",
    "practice_group_id", "practice_group_size", "other_doctors_at_location",
]


def _iso(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def profile_payload_row(prospect, score, lead, *, other_doctors: Sequence[str] = ()) -> dict:
    """Build one wire profile row from a prospect, its latest workflow_score, and its lead.

    score/lead may be None (e.g. a prospect not yet scored or contacted); fields degrade to
    sensible defaults. missing_profile_fields and other_doctors are joined to the documented
    string forms.
    """
    missing = ",".join(getattr(score, "missing_profile_fields", None) or []) if score else ""
    return {
        "npi": prospect.npi,
        "profile_url": getattr(prospect, "profile_url", None) or "",
        "profile_slug": getattr(prospect, "profile_slug", None) or "",
        "claim_url": (getattr(lead, "claim_url", None) or "") if lead else "",
        "display_name": prospect.display_name,
        "first_name": prospect.first_name,
        "last_name": prospect.last_name,
        "credential": prospect.credential,
        "specialty": prospect.primary_specialty,
        "taxonomy_code": prospect.primary_taxonomy_code,
        "city": prospect.practice_city,
        "state": prospect.practice_state,
        "practice_zip": prospect.practice_zip,
        "practice_phone": prospect.practice_phone,
        "source": prospect.source,
        "source_fetched_at": _iso(getattr(prospect, "source_fetched_at", None)),
        "campaign": (score.campaign if score else "") or "",
        "activation_status": lead.activation_status if lead else "not_contacted",
        "activation_priority": (score.activation_priority if score else "") or "",
        "activation_score": str(score.activation_score) if score else "0",
        "priority_reason": (score.priority_reason if score else "") or "",
        "profile_completeness_score": str(score.profile_completeness_score) if score else "0",
        "missing_profile_fields": missing,
        "practice_group_id": (getattr(score, "practice_group_id", None) or "") if score else "",
        "practice_group_size": str(getattr(score, "practice_group_size", 0) or 0) if score else "0",
        "other_doctors_at_location": " | ".join(other_doctors),
    }


def build_payload(
    profiles: Iterable[dict],
    *,
    campaign: str,
    generated_at: str,
    source: str = "cms_nppes_registry_api",
    dry_run: bool = True,
) -> dict:
    """Wrap profile rows in the request envelope. generated_at is passed in (caller stamps it)."""
    return {
        "dry_run": dry_run,
        "generated_at": generated_at,
        "source": source,
        "campaign": campaign,
        "profiles": list(profiles),
    }
