"""Deterministic profile/claim URL derivation. (monolith :1526-1539, :1242-1259)

CERTUMALINK_BASE_URL is injectable; the default matches the monolith. The '-{npi}' slug
suffix guarantees uniqueness/idempotency. claim_urls_by_npi parses the publish response.
"""
from __future__ import annotations

import re
from typing import Mapping

from .models import DoctorRecord
from .util import clean

__all__ = ["DEFAULT_BASE_URL", "slugify", "profile_slug", "profile_url", "claim_urls_by_npi"]

DEFAULT_BASE_URL = "https://www.certumalink.com"


def slugify(value: str) -> str:
    """ascii-lower, non-alnum -> '-', collapse repeats, trim. (monolith _slugify, :1536-1539)"""
    lower = value.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")
    return re.sub(r"-+", "-", slug)


def profile_slug(record: DoctorRecord) -> str:
    """slug(first last | display_name) + '-' + npi. (monolith _profile_slug, :1530-1533)"""
    name = " ".join(part for part in (record.first_name, record.last_name) if part) or record.display_name
    base = slugify(name) or "doctor"
    return f"{base}-{record.npi}"


def profile_url(record: DoctorRecord, base_url: str = DEFAULT_BASE_URL) -> str:
    """{base}/doctors/{slug}. (monolith _profile_url, :1526-1527)"""
    return f"{base_url}/doctors/{profile_slug(record)}"


def claim_urls_by_npi(publish_result: dict[str, object] | None) -> dict[str, str]:
    """Extract {npi: claim_url} from a publish response. (monolith _claim_urls_by_npi, :1242-1259)"""
    if publish_result is None:
        return {}
    response = publish_result.get("response")
    if not isinstance(response, Mapping):
        return {}
    results = response.get("results", [])
    if not isinstance(results, list):
        return {}
    claim_urls: dict[str, str] = {}
    for item in results:
        if not isinstance(item, Mapping):
            continue
        npi = clean(item.get("npi"))
        claim_url = clean(item.get("claim_url"))
        if npi and claim_url:
            claim_urls[npi] = claim_url
    return claim_urls
