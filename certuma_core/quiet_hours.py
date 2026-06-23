"""Quiet-hours by physician practice state (Phase 1 task P1.3, pure).

Business hours are Mon-Fri 08:00-17:00 local. Outside that (incl. weekends) is quiet -> the Gate
HOLDs. Multi-timezone states use the WIDEST quiet window: it is quiet if quiet in ANY of the
state's zones (so a send only goes out when it is business hours across the whole state). An
unknown/blank state fails safe (treated as quiet). Window is a placeholder; per-campaign override
is a later seam (plan R10).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

__all__ = ["STATE_TZS", "is_quiet_hours", "BUSINESS_START_HOUR", "BUSINESS_END_HOUR"]

BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 17  # exclusive

# state -> the IANA zone(s) covering it (multi-zone states list all relevant zones)
STATE_TZS: dict[str, tuple[str, ...]] = {
    "AL": ("America/Chicago",), "AK": ("America/Anchorage",), "AZ": ("America/Phoenix",),
    "AR": ("America/Chicago",), "CA": ("America/Los_Angeles",), "CO": ("America/Denver",),
    "CT": ("America/New_York",), "DE": ("America/New_York",), "DC": ("America/New_York",),
    "FL": ("America/New_York", "America/Chicago"), "GA": ("America/New_York",),
    "HI": ("Pacific/Honolulu",), "ID": ("America/Denver", "America/Los_Angeles"),
    "IL": ("America/Chicago",), "IN": ("America/Indiana/Indianapolis", "America/Chicago"),
    "IA": ("America/Chicago",), "KS": ("America/Chicago", "America/Denver"),
    "KY": ("America/New_York", "America/Chicago"), "LA": ("America/Chicago",),
    "ME": ("America/New_York",), "MD": ("America/New_York",), "MA": ("America/New_York",),
    "MI": ("America/Detroit", "America/Menominee"), "MN": ("America/Chicago",),
    "MS": ("America/Chicago",), "MO": ("America/Chicago",),
    "MT": ("America/Denver",), "NE": ("America/Chicago", "America/Denver"),
    "NV": ("America/Los_Angeles",), "NH": ("America/New_York",), "NJ": ("America/New_York",),
    "NM": ("America/Denver",), "NY": ("America/New_York",), "NC": ("America/New_York",),
    "ND": ("America/Chicago", "America/Denver"), "OH": ("America/New_York",),
    "OK": ("America/Chicago",), "OR": ("America/Los_Angeles", "America/Boise"),
    "PA": ("America/New_York",), "RI": ("America/New_York",), "SC": ("America/New_York",),
    "SD": ("America/Chicago", "America/Denver"), "TN": ("America/Chicago", "America/New_York"),
    "TX": ("America/Chicago", "America/Denver"), "UT": ("America/Denver",),
    "VT": ("America/New_York",), "VA": ("America/New_York",), "WA": ("America/Los_Angeles",),
    "WV": ("America/New_York",), "WI": ("America/Chicago",), "WY": ("America/Denver",),
}


def _is_business(local: datetime) -> bool:
    return local.weekday() < 5 and BUSINESS_START_HOUR <= local.hour < BUSINESS_END_HOUR


def is_quiet_hours(state: str, when_utc: datetime, tz_map: dict = STATE_TZS) -> bool:
    """True if `when_utc` is outside business hours for the state (any zone -> quiet)."""
    zones = tz_map.get((state or "").strip().upper())
    if not zones:
        return True  # unknown / blank state fails safe
    for zone in zones:
        if not _is_business(when_utc.astimezone(ZoneInfo(zone))):
            return True
    return False
