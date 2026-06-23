"""Specialty filtering — the single shared predicate for import gating AND scoring boost.

Lifted verbatim from the monolith (:909-924). Preserve the asymmetry: substring match on
specialty text, exact `==` match on taxonomy code; an empty filter list => True.
"""
from __future__ import annotations

from .models import CampaignPreset, DoctorRecord
from .util import dedupe

__all__ = ["combined_specialty_filters", "matches_specialty"]


def combined_specialty_filters(
    specialty_filters: list[str],
    campaign: CampaignPreset | None,
) -> list[str]:
    """Merge explicit filters with a campaign's terms, order-preserving. (monolith :909-916)"""
    terms = list(specialty_filters)
    if campaign is not None:
        terms.extend(campaign.specialty_terms)
    return dedupe(terms)


def matches_specialty(record: DoctorRecord, specialty_filters: list[str] | None) -> bool:
    """True if no filter, or any term substring-matches specialty / exact-matches taxonomy.

    (monolith _matches_specialty, :919-924)
    """
    if not specialty_filters:
        return True
    specialty = record.primary_specialty.lower()
    taxonomy = record.primary_taxonomy_code.lower()
    return any(term in specialty or term == taxonomy for term in specialty_filters)
