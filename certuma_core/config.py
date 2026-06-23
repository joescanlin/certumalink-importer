"""Scoring configuration — the monolith's magic numbers, made injectable.

The defaults reproduce portable/certumalink-doctor-import.py::_workflow_fields (:1321-1391)
EXACTLY (pinned by a frozen snapshot test). The rubric is deliberately deliverability-blind
today (biggest weight is phone=25); the `features` hook on the scorer is where email-engagement
features will later re-weight without a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ScoringConfig", "DEFAULT_SCORING_CONFIG"]


@dataclass(frozen=True)
class ScoringConfig:
    # additive weights (monolith :1332-1355)
    phone: int = 25
    both_names: int = 10
    specialty_and_taxonomy: int = 15
    full_address: int = 15
    shared_practice: int = 5
    fresh_contact: int = 5
    completeness_high_bonus: int = 10
    completeness_low_penalty: int = 10
    # thresholds (monolith :1352-1354, :1366-1369)
    completeness_high_threshold: int = 90
    completeness_low_threshold: int = 70
    high_tier: int = 75
    medium_tier: int = 50
    # statuses that count as "fresh contact" for the +fresh_contact bump (monolith L1349)
    fresh_contact_statuses: tuple[str, ...] = ("not_contacted", "queued_today")


DEFAULT_SCORING_CONFIG = ScoringConfig()
