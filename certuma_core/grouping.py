"""Practice grouping — cluster doctors by normalized shared phone+address.

Lifted from the monolith (:1262-1318). practice_group_rows drops the monolith's joined
`doctors`/`npi_list` string columns (membership is derived from the prospect FK in Postgres);
group membership is exposed structurally via build_practice_groups / group_by_npi instead.

NOTE (carried risk): practice_group_key includes address_2 (suite line) with no address
standardization, so it over-fragments groups. Preserved for parity; flag before autonomy widens.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict

from .models import DoctorRecord, PracticeGroup
from .util import clean, digits_only

__all__ = [
    "build_practice_groups",
    "group_by_npi",
    "practice_group_key",
    "practice_group_id",
    "practice_group_rows",
]


def practice_group_key(record: DoctorRecord) -> str:
    """Normalized phone+address key; falls back to NPI when both are empty. (monolith :1282-1292)"""
    phone = digits_only(record.practice_phone)
    address_parts = [
        record.practice_address_1,
        record.practice_address_2,
        record.practice_city,
        record.practice_state,
        record.practice_zip,
    ]
    address = "|".join(clean(part).lower() for part in address_parts)
    return f"{phone}|{address}" if phone or address.strip("|") else record.npi


def practice_group_id(key: str) -> str:
    """'practice-' + first 10 hex of sha1(key). (monolith :1295-1297)"""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"practice-{digest}"


def build_practice_groups(records: list[DoctorRecord]) -> list[PracticeGroup]:
    """Group records by practice_group_key, preserving first-seen order. (monolith :1262-1271)"""
    groups_by_key: "OrderedDict[str, PracticeGroup]" = OrderedDict()
    for record in records:
        key = practice_group_key(record)
        group = groups_by_key.get(key)
        if group is None:
            group = PracticeGroup(group_id=practice_group_id(key), records=[])
            groups_by_key[key] = group
        group.records.append(record)
    return list(groups_by_key.values())


def group_by_npi(practice_groups: list[PracticeGroup]) -> dict[str, PracticeGroup]:
    """NPI -> its PracticeGroup. (monolith :1274-1279)"""
    groups: dict[str, PracticeGroup] = {}
    for group in practice_groups:
        for record in group.records:
            groups[record.npi] = group
    return groups


def practice_group_rows(practice_groups: list[PracticeGroup]) -> list[dict[str, object]]:
    """UPSERT-row builder for the `practice_group` table, sorted by -size then id.

    Representative phone/address = the first record in the group (monolith parity, :1300-1318).
    The joined `doctors`/`npi_list` columns are intentionally NOT emitted.
    """
    rows: list[dict[str, object]] = []
    for group in sorted(practice_groups, key=lambda item: (-item.size, item.group_id)):
        first = group.records[0]
        rows.append(
            {
                "practice_group_id": group.group_id,
                "practice_group_size": group.size,
                "practice_phone": first.practice_phone,
                "practice_address_1": first.practice_address_1,
                "practice_address_2": first.practice_address_2,
                "practice_city": first.practice_city,
                "practice_state": first.practice_state,
                "practice_zip": first.practice_zip,
            }
        )
    return rows
