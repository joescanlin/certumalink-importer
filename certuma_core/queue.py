"""Daily work-queue ranking — retyped from the monolith's dict-based _rox_today_rows (:1411-1430).

Operates on typed QueueItem objects instead of dict[str,str] (kills the int(row[...] or '0')
coercion smell). Eligibility (state-based) is decoupled from ranking (score-based). Default
exclusion is the core's QUEUE_EXCLUDED_STATES; for legacy data this matches the monolith
because `exhausted` never appears in the 9 legacy statuses.
"""
from __future__ import annotations

from dataclasses import dataclass

from .status import QUEUE_EXCLUDED_STATES

__all__ = ["PRIORITY_RANK", "QueueItem", "RankedQueueItem", "rank_queue"]

# monolith priority_rank (L1417)
PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class QueueItem:
    npi: str
    doctor_name: str
    activation_status: str
    activation_priority: str
    activation_score: int


@dataclass(frozen=True)
class RankedQueueItem:
    queue_rank: int
    item: QueueItem


def rank_queue(
    items: list[QueueItem],
    *,
    excluded_states: frozenset[str] | set[str] = QUEUE_EXCLUDED_STATES,
    priority_rank: dict[str, int] = PRIORITY_RANK,
) -> list[RankedQueueItem]:
    """Filter out excluded states + 'low' priority, then sort and assign 1-based queue_rank.

    Sort key matches the monolith exactly: (priority_rank, -score, doctor_name, npi).
    """
    eligible = [
        item
        for item in items
        if item.activation_status not in excluded_states and item.activation_priority != "low"
    ]
    ordered = sorted(
        eligible,
        key=lambda item: (
            priority_rank.get(item.activation_priority, 9),
            -item.activation_score,
            item.doctor_name,
            item.npi,
        ),
    )
    return [RankedQueueItem(queue_rank=index, item=item) for index, item in enumerate(ordered, start=1)]
