"""Campaign presets — the seed that will hydrate the `campaign` table in Phase 0.

CAMPAIGN_PRESETS is lifted verbatim from the monolith (:288-326). get_campaign raises a
typed CampaignNotFound rather than the monolith's bare KeyError (_campaign_for_name :903-906).
"""
from __future__ import annotations

from .models import CampaignPreset

__all__ = ["CAMPAIGN_PRESETS", "CampaignNotFound", "get_campaign", "campaign_or_none", "list_campaigns"]


class CampaignNotFound(KeyError):
    """Raised when a campaign name is not a known preset."""


CAMPAIGN_PRESETS: dict[str, CampaignPreset] = {
    "primary-care": CampaignPreset(
        name="primary-care",
        label="Primary Care",
        specialty_terms=(
            "family medicine",
            "internal medicine",
            "general practice",
            "pediatrics",
            "207q00000x",
            "207r00000x",
            "208000000x",
            "208d00000x",
        ),
        priority_boost=18,
        pitch_angle="primary care practice",
    ),
    "dermatology": CampaignPreset(
        name="dermatology",
        label="Dermatology",
        specialty_terms=("dermatology", "207n00000x"),
        priority_boost=22,
        pitch_angle="dermatology practice",
    ),
    "cardiology": CampaignPreset(
        name="cardiology",
        label="Cardiology",
        specialty_terms=("cardiology", "cardiovascular disease", "207rc0000x"),
        priority_boost=22,
        pitch_angle="cardiology practice",
    ),
    "urgent-care": CampaignPreset(
        name="urgent-care",
        label="Urgent Care",
        specialty_terms=("urgent care", "emergency medicine", "family medicine", "207p00000x", "207q00000x"),
        priority_boost=18,
        pitch_angle="urgent care practice",
    ),
}


def get_campaign(name: str) -> CampaignPreset:
    try:
        return CAMPAIGN_PRESETS[name]
    except KeyError as exc:
        raise CampaignNotFound(name) from exc


def campaign_or_none(name: str | None) -> CampaignPreset | None:
    """Monolith _campaign_for_name parity: None -> None, otherwise look up (raises if unknown)."""
    if name is None:
        return None
    return get_campaign(name)


def list_campaigns() -> list[CampaignPreset]:
    return list(CAMPAIGN_PRESETS.values())
