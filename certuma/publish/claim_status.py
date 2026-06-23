"""Claim-status polling stub (Phase 0 task B13, decision #7).

The platform's activation webhook does not exist yet, so until it does we POLL claim_url status.
This module is the seam: poll_claim_urls drives a per-lead status check through an injected
`fetch` callable. The default fetch raises ClaimStatusUnavailable (the endpoint is not built),
which makes the not-yet-wired state explicit rather than silently returning "no activation".
When the read/query endpoint exists, pass a real fetch; the poller (Phase 1) is the only actor
allowed to drive interested -> physician_activated (see certuma_core.status.ACTIVATION_ONLY_ACTORS).
"""
from __future__ import annotations

from typing import Callable, Iterable, Tuple

__all__ = ["ClaimStatusUnavailable", "default_fetch", "poll_claim_urls"]


class ClaimStatusUnavailable(RuntimeError):
    """Raised when no claim-status source is wired (the platform endpoint is not built yet)."""


def default_fetch(claim_url: str) -> str:
    raise ClaimStatusUnavailable(
        "claim-status endpoint not built; inject a real fetch when the platform read API exists"
    )


def poll_claim_urls(
    items: Iterable[Tuple[str, str]],
    *,
    fetch: Callable[[str], str] = default_fetch,
) -> dict:
    """Map npi -> claim status for each (npi, claim_url). Leads with no claim_url are skipped."""
    out: dict[str, str] = {}
    for npi, claim_url in items:
        if not claim_url:
            continue
        out[npi] = fetch(claim_url)
    return out
