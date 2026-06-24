"""Customer-Intelligence queries (Phase 3 task P3.1).

Read-only analytics over the `reporting` schema: the full universe -> enriched -> sent -> delivered
-> opened -> replied -> activated funnel, sliced by specialty / region / campaign, plus conversion
rates, time-to-activation, and unit economics. Suppressed clinicians are excluded from every metric
(PII governance). All queries take an already-built reporting schema (see elt.rebuild).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["DIMENSIONS", "funnel_totals", "by_dimension", "unit_economics",
           "time_to_activation_days", "rebuilt_at", "touches_by_channel"]

# whitelist of group-by columns (the value is interpolated into SQL, so it MUST be validated)
DIMENSIONS = {"specialty", "state", "campaign"}

_FUNNEL = """
SELECT
  count(*)                                AS universe,
  count(*) FILTER (WHERE has_contact)     AS enriched,
  count(*) FILTER (WHERE sent)            AS sent,
  count(*) FILTER (WHERE delivered)       AS delivered,
  count(*) FILTER (WHERE opened)          AS opened,
  count(*) FILTER (WHERE replied)         AS replied,
  count(*) FILTER (WHERE activated)       AS activated
FROM reporting.fact_lead_funnel
WHERE NOT is_suppressed;
"""


def _rate(num: int, den: int) -> Optional[float]:
    return round(100.0 * num / den, 1) if den else None


def funnel_totals(session: Session) -> dict:
    r = session.execute(text(_FUNNEL)).mappings().one()
    d = dict(r)
    d["delivery_rate"] = _rate(d["delivered"], d["sent"])
    d["open_rate"] = _rate(d["opened"], d["delivered"])
    d["reply_rate"] = _rate(d["replied"], d["delivered"])
    d["activation_rate"] = _rate(d["activated"], d["sent"])
    return d


def by_dimension(session: Session, dimension: str, *, limit: int = 25) -> List[dict]:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}")
    sql = f"""
    SELECT COALESCE(NULLIF({dimension}, ''), '(unknown)') AS label,
           count(*)                          AS leads,
           count(*) FILTER (WHERE sent)      AS sent,
           count(*) FILTER (WHERE replied)   AS replied,
           count(*) FILTER (WHERE activated) AS activated
    FROM reporting.fact_lead_funnel
    WHERE NOT is_suppressed
    GROUP BY 1
    ORDER BY activated DESC, sent DESC
    LIMIT :limit;
    """
    rows = []
    for r in session.execute(text(sql), {"limit": limit}).mappings():
        d = dict(r)
        d["activation_rate"] = _rate(d["activated"], d["sent"])
        d["reply_rate"] = _rate(d["replied"], d["sent"])
        rows.append(d)
    return rows


def unit_economics(session: Session) -> dict:
    touch = session.execute(text(
        "SELECT COALESCE(sum(send_cost), 0) AS total_cost, count(*) AS touches FROM reporting.fact_touch"
    )).mappings().one()
    activations = session.execute(text(
        "SELECT count(*) FROM reporting.fact_lead_funnel WHERE activated AND NOT is_suppressed"
    )).scalar() or 0
    total_cost = float(touch["total_cost"])
    return {
        "total_send_cost": round(total_cost, 2),
        "touches": touch["touches"],
        "activations": activations,
        "cost_per_activation": round(total_cost / activations, 2) if activations else None,
    }


def time_to_activation_days(session: Session) -> Optional[float]:
    v = session.execute(text(
        "SELECT avg(extract(epoch FROM (activated_at - first_sent_at)) / 86400.0) "
        "FROM reporting.fact_lead_funnel "
        "WHERE activated AND first_sent_at IS NOT NULL AND activated_at IS NOT NULL"
    )).scalar()
    return round(float(v), 1) if v is not None else None


def rebuilt_at(session: Session):
    return session.execute(text("SELECT rebuilt_at FROM reporting.meta WHERE id = 1")).scalar()


def touches_by_channel(session: Session) -> List[dict]:
    return [dict(r) for r in session.execute(text(
        "SELECT channel, count(*) AS touches, count(*) FILTER (WHERE delivered) AS delivered "
        "FROM reporting.fact_touch GROUP BY channel ORDER BY touches DESC"
    )).mappings()]
