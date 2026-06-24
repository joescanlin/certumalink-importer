"""Variant performance + winner (Phase 3 task P3.7) - the DB wrapper over the learning loop.

variant_performance attributes each lead's outcome to its (single, stable) message variant and
computes per-variant sent / replied / activated + rates. winning_variant surfaces the best one once
it has enough sample - measured and surfaced for the operator to promote (auto-promote is off by
default). Read-only.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from certuma_core.learning import MIN_SAMPLE, pick_winner

__all__ = ["variant_performance", "winning_variant"]

_SQL = """
SELECT m.variant_id AS variant,
       count(DISTINCT l.id) AS sent,
       count(DISTINCT l.id) FILTER (WHERE l.activation_status = 'physician_activated') AS activated,
       count(DISTINCT l.id) FILTER (
           WHERE EXISTS (SELECT 1 FROM message i WHERE i.lead_id = l.id AND i.direction = 'inbound')
       ) AS replied
FROM message m
JOIN lead l ON l.id = m.lead_id
WHERE m.direction = 'outbound' AND m.variant_id IS NOT NULL AND m.variant_id <> ''
  AND (CAST(:campaign AS text) IS NULL OR l.campaign = :campaign)
GROUP BY m.variant_id
ORDER BY activated DESC, sent DESC;
"""


def variant_performance(session: Session, *, campaign: Optional[str] = None) -> List[dict]:
    rows = [dict(r) for r in session.execute(text(_SQL), {"campaign": campaign}).mappings()]
    for r in rows:
        sent = r["sent"] or 0
        r["activation_rate"] = round(100.0 * r["activated"] / sent, 1) if sent else None
        r["reply_rate"] = round(100.0 * r["replied"] / sent, 1) if sent else None
    return rows


def winning_variant(session: Session, *, campaign: Optional[str] = None,
                    min_sample: int = MIN_SAMPLE) -> Optional[str]:
    winner = pick_winner(variant_performance(session, campaign=campaign), min_sample=min_sample)
    return winner["variant"] if winner else None
