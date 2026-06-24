"""Learning loop / variant optimization (Phase 3 task P3.7) - pure, no DB.

assign_variant gives each clinician a STABLE variant (keyed by npi), so a lead never switches variant
mid-experiment - which also makes attribution trivial: a lead's outcome belongs to its single variant
(the chosen last-touch attribution model). pick_winner selects the best variant by activation rate
once a minimum sample is reached. Per the approved decision the loop MEASURES and SURFACES the winner
for the operator to promote; auto-promotion is a per-campaign flag defaulting off (handled by the
caller).
"""
from __future__ import annotations

import hashlib
from typing import List, Optional, Sequence

__all__ = ["MIN_SAMPLE", "assign_variant", "pick_winner"]

MIN_SAMPLE = 20  # a variant needs at least this many sends before it can win (provisional)


def assign_variant(items: Sequence, key: str):
    """Deterministically assign one item from `items` to `key` (stable per key). items must be non-empty."""
    if not items:
        raise ValueError("assign_variant needs at least one item")
    idx = int(hashlib.md5(str(key).encode()).hexdigest(), 16) % len(items)
    return items[idx]


def pick_winner(stats: List[dict], *, min_sample: int = MIN_SAMPLE) -> Optional[dict]:
    """The variant with the best activation rate among those with enough sample; None if none qualify.

    Each stat is {variant, sent, activated, replied, ...}. Ties on activation rate break on reply rate
    then on sample size.
    """
    eligible = [s for s in stats if s.get("sent", 0) >= min_sample]
    if not eligible:
        return None

    def _key(s: dict):
        sent = s.get("sent", 0) or 1
        return (s.get("activated", 0) / sent, s.get("replied", 0) / sent, s.get("sent", 0))

    return max(eligible, key=_key)
