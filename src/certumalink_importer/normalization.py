from __future__ import annotations

from typing import Mapping

from .models import DoctorRecord
from .zipcodes import normalize_zip_code


PHYSICIAN_TAXONOMY_PREFIXES = ("207", "208")


def normalize_result(
    result: Mapping[str, object],
    matched_zip: str,
    fetched_at: str,
) -> DoctorRecord | None:
    if str(result.get("enumeration_type") or "").upper() not in ("", "NPI-1"):
        return None

    basic = _mapping(result.get("basic"))
    if not _is_active(basic):
        return None

    taxonomy = _select_physician_taxonomy(result.get("taxonomies"))
    if taxonomy is None:
        return None

    query_zip = normalize_zip_code(matched_zip)
    address = _select_practice_address(result.get("addresses"), matched_zip=query_zip)
    if address is None:
        return None

    npi = _clean(result.get("number"))
    if not npi:
        return None

    first_name = _clean(basic.get("first_name"))
    middle_name = _clean(basic.get("middle_name"))
    last_name = _clean(basic.get("last_name"))
    credential = _clean(basic.get("credential"))
    display_name = _display_name(first_name, middle_name, last_name, credential)

    record = DoctorRecord(
        npi=npi,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        credential=credential,
        display_name=display_name,
        primary_taxonomy_code=_clean(taxonomy.get("code")),
        primary_specialty=_clean(taxonomy.get("desc")),
        practice_address_1=_clean(address.get("address_1")),
        practice_address_2=_clean(address.get("address_2")),
        practice_city=_clean(address.get("city")),
        practice_state=_clean(address.get("state")),
        practice_zip=_normalize_address_zip(address.get("postal_code")),
        practice_phone=_clean(address.get("telephone_number")),
        matched_zips=[],
        source_fetched_at=fetched_at,
    )
    record.add_matched_zip(query_zip)
    return record


def _is_active(basic: Mapping[str, object]) -> bool:
    status = _clean(basic.get("status")).upper()
    if status and status != "A":
        return False
    if _clean(basic.get("deactivation_date")):
        return False
    if _clean(basic.get("deactivation_reason_code")):
        return False
    return True


def _select_physician_taxonomy(value: object) -> Mapping[str, object] | None:
    taxonomies = [_mapping(item) for item in _list(value)]
    physician_taxonomies = [
        taxonomy for taxonomy in taxonomies if _is_physician_taxonomy(_clean(taxonomy.get("code")))
    ]
    if not physician_taxonomies:
        return None
    for taxonomy in physician_taxonomies:
        if _is_truthy(taxonomy.get("primary")):
            return taxonomy
    return physician_taxonomies[0]


def _select_practice_address(value: object, *, matched_zip: str) -> Mapping[str, object] | None:
    addresses = [_mapping(item) for item in _list(value)]
    if not addresses:
        return None
    us_addresses = [
        address
        for address in addresses
        if _clean(address.get("country_code")).upper() in ("", "US")
    ]
    candidates = us_addresses or addresses
    for address in candidates:
        if (
            _clean(address.get("address_purpose")).upper() == "LOCATION"
            and _normalize_address_zip(address.get("postal_code")) == matched_zip
        ):
            return address
    return None


def _is_physician_taxonomy(code: str) -> bool:
    return code.startswith(PHYSICIAN_TAXONOMY_PREFIXES)


def _display_name(first: str, middle: str, last: str, credential: str) -> str:
    name_parts = [part for part in (first, middle, last) if part]
    display = " ".join(name_parts)
    if credential:
        return f"{display}, {credential}" if display else credential
    return display


def _normalize_address_zip(value: object) -> str:
    text = _clean(value)
    if len(text) >= 5 and text[:5].isdigit():
        return text[:5]
    return text


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y")
