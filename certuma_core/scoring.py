"""Activation scoring + profile completeness.

compute_workflow_fields reproduces the monolith's _workflow_fields (:1321-1391) exactly when
given DEFAULT_SCORING_CONFIG; every weight/threshold is now an injected ScoringConfig field.
The `features` hook is reserved for future email-engagement signals (the rubric is currently
deliverability-blind). Reason strings are behavior and are pinned literals.
"""
from __future__ import annotations

from typing import Iterable, Optional

from .config import DEFAULT_SCORING_CONFIG, ScoringConfig
from .models import CampaignPreset, DoctorRecord, PracticeGroup, WorkflowFields
from .specialty import matches_specialty
from .util import clean

__all__ = [
    "COMPLETENESS_FIELDS",
    "profile_completeness",
    "compute_workflow_fields",
    "priority_counts",
    "average_profile_completeness",
]

# (label, DoctorRecord attribute) — the 9-field checklist. (monolith _profile_completeness, :1394-1408)
COMPLETENESS_FIELDS: tuple[tuple[str, str], ...] = (
    ("first_name", "first_name"),
    ("last_name", "last_name"),
    ("specialty", "primary_specialty"),
    ("taxonomy_code", "primary_taxonomy_code"),
    ("practice_address_1", "practice_address_1"),
    ("practice_city", "practice_city"),
    ("practice_state", "practice_state"),
    ("practice_zip", "practice_zip"),
    ("practice_phone", "practice_phone"),
)


def profile_completeness(record: DoctorRecord) -> tuple[int, list[str]]:
    """(0-100 score, missing-label list). (monolith _profile_completeness, :1394-1408)"""
    missing = [label for label, attr in COMPLETENESS_FIELDS if not clean(getattr(record, attr))]
    present = len(COMPLETENESS_FIELDS) - len(missing)
    return round((present / len(COMPLETENESS_FIELDS)) * 100), missing


def compute_workflow_fields(
    record: DoctorRecord,
    *,
    activation_status: str,
    campaign: CampaignPreset | None,
    practice_group: PracticeGroup,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
    features: Optional[object] = None,  # reserved: future email-engagement re-weighting
) -> WorkflowFields:
    completeness_score, missing_fields = profile_completeness(record)
    score = 0
    reasons: list[str] = []

    if record.practice_phone:
        score += config.phone
        reasons.append("has practice phone")
    else:
        reasons.append("missing practice phone")
    if record.first_name and record.last_name:
        score += config.both_names
    if record.primary_specialty and record.primary_taxonomy_code:
        score += config.specialty_and_taxonomy
    if record.practice_address_1 and record.practice_city and record.practice_state and record.practice_zip:
        score += config.full_address
    if campaign is not None and matches_specialty(record, list(campaign.specialty_terms)):
        score += campaign.priority_boost
        reasons.append(f"matches {campaign.label} campaign")
    if practice_group.size > 1:
        score += config.shared_practice
        reasons.append(f"shared practice with {practice_group.size} doctors")
    if activation_status in config.fresh_contact_statuses:
        score += config.fresh_contact
        reasons.append("not contacted yet")
    if completeness_score >= config.completeness_high_threshold:
        score += config.completeness_high_bonus
    elif completeness_score < config.completeness_low_threshold:
        score -= config.completeness_low_penalty

    # Priority gating — override order is load-bearing (monolith L1357-1374). Do not reorder.
    if activation_status == "do_not_contact":
        priority = "low"
        reason = "status is do_not_contact"
    elif activation_status == "physician_activated":
        priority = "low"
        reason = "already activated"
    elif activation_status == "needs_review" or completeness_score < config.completeness_low_threshold:
        priority = "low"
        reason = f"needs review: missing {', '.join(missing_fields) or 'profile data'}"
    elif score >= config.high_tier:
        priority = "high"
        reason = "; ".join(reasons[:3]) or "high activation fit"
    elif score >= config.medium_tier:
        priority = "medium"
        reason = "; ".join(reasons[:3]) or "moderate activation fit"
    else:
        priority = "low"
        reason = "; ".join(reasons[:3]) or "low activation fit"

    other_doctors = tuple(
        other.display_name for other in practice_group.records if other.npi != record.npi
    )
    return WorkflowFields(
        campaign=campaign.name if campaign is not None else "",
        activation_priority=priority,
        activation_score=max(score, 0),
        priority_reason=reason,
        full_priority_reasons=tuple(reasons),
        profile_completeness_score=completeness_score,
        missing_profile_fields=tuple(missing_fields),
        practice_group_id=practice_group.group_id,
        practice_group_size=practice_group.size,
        other_doctors_at_location=other_doctors,
    )


def priority_counts(workflows: Iterable[WorkflowFields]) -> dict[str, int]:
    """Tally of activation_priority. (monolith _priority_counts, :1433-1437)"""
    counts: dict[str, int] = {}
    for workflow in workflows:
        counts[workflow.activation_priority] = counts.get(workflow.activation_priority, 0) + 1
    return counts


def average_profile_completeness(workflows: Iterable[WorkflowFields]) -> int:
    """Mean completeness, 0 on empty. (monolith _average_profile_completeness, :1440-1444)"""
    values = [workflow.profile_completeness_score for workflow in workflows]
    if not values:
        return 0
    return round(sum(values) / len(values))
