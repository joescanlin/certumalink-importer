"""Small pure string helpers shared across the core (monolith _clean/_digits_only/_dedupe)."""
from __future__ import annotations

import re

__all__ = ["clean", "digits_only", "dedupe"]


def clean(value: object) -> str:
    """Trim to a string; None -> ''. (monolith _clean, :1808-1809)"""
    return "" if value is None else str(value).strip()


def digits_only(value: object) -> str:
    """Strip every non-digit. (monolith _digits_only, :1447-1448)"""
    return re.sub(r"\D+", "", "" if value is None else str(value))


def dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication. (monolith _dedupe, :1790-1797)"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out
