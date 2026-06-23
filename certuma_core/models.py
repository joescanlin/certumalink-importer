"""Core domain dataclasses, lifted from the monolith.

DoctorRecord drops the monolith's to_export_row() (CSV coupling). WorkflowFields is
*retyped*: missing_profile_fields / other_doctors_at_location / full_priority_reasons are
now tuples (not pre-joined strings) so Postgres columns and audit get the lossless form;
the legacy joined strings are reproducible via the helpers below for golden parity.
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SOURCE", "DoctorRecord", "PracticeGroup", "CampaignPreset", "WorkflowFields"]

SOURCE = "cms_nppes_registry_api"


@dataclass
class DoctorRecord:
    """A normalized individual physician seed record. (monolith DoctorRecord, :150-193)"""

    npi: str
    first_name: str
    middle_name: str
    last_name: str
    credential: str
    display_name: str
    primary_taxonomy_code: str
    primary_specialty: str
    practice_address_1: str
    practice_address_2: str
    practice_city: str
    practice_state: str
    practice_zip: str
    practice_phone: str
    source_fetched_at: str
    matched_zips: list[str] = field(default_factory=list)
    source: str = SOURCE

    def add_matched_zip(self, zip_code: str) -> None:
        if zip_code not in self.matched_zips:
            self.matched_zips.append(zip_code)


@dataclass
class PracticeGroup:
    """Doctors sharing a normalized practice phone+address. (monolith PracticeGroup, :265-272)"""

    group_id: str
    records: list[DoctorRecord]

    @property
    def size(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class CampaignPreset:
    """A vertical-targeting preset. (monolith CampaignPreset, :256-262)"""

    name: str
    label: str
    specialty_terms: tuple[str, ...]
    priority_boost: int
    pitch_angle: str


@dataclass(frozen=True)
class WorkflowFields:
    """Deterministic scoring + grouping output for one (record, status, campaign).

    Retyped from the monolith's all-string WorkflowFields (:275-285): the list-shaped
    fields are tuples here. Use the *_joined helpers to reproduce the legacy CSV strings.
    """

    campaign: str
    activation_priority: str
    activation_score: int
    priority_reason: str
    full_priority_reasons: tuple[str, ...]
    profile_completeness_score: int
    missing_profile_fields: tuple[str, ...]
    practice_group_id: str
    practice_group_size: int
    other_doctors_at_location: tuple[str, ...]

    @property
    def missing_profile_fields_joined(self) -> str:
        """Monolith parity form: ','.join(missing). (monolith L1387)"""
        return ",".join(self.missing_profile_fields)

    @property
    def other_doctors_joined(self) -> str:
        """Monolith parity form: ' | '.join(other doctors). (monolith L1390)"""
        return " | ".join(self.other_doctors_at_location)
