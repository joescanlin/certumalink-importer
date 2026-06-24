"""Reporting ELT (Phase 3 task P3.0).

rebuild() materializes the read-optimized `reporting` schema from the operational tables in one
deterministic pass (full delete + insert-select). It is a READER of operational state and the only
writer of `reporting`; it never touches operational rows, so analytics can never corrupt the ledger.
Suppression flags ride along so the analytics + the Series-A export inherit opt-out (latent issue 2).
The caller owns the transaction; run_rebuild opens one, rebuilds, and commits (`make rebuild`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from certuma.config import Settings, get_settings
from certuma.observability import METRICS, emit, get_logger

__all__ = ["SEND_COST", "RebuildReport", "rebuild", "run_rebuild", "main"]

_LOG = get_logger("certuma.reporting")

SEND_COST = 0.02  # provisional per-send unit cost (USD) for unit-economics analytics

_TABLES = ("dim_clinician", "dim_campaign", "fact_touch", "fact_event", "fact_lead_funnel")

# Order matters only for readability; all are independent full rebuilds.
_DELETES = [f"DELETE FROM reporting.{t};" for t in _TABLES]

_DIM_CLINICIAN = """
INSERT INTO reporting.dim_clinician
    (npi, display_name, specialty, state, city, practice_group_id, group_size, is_suppressed)
SELECT p.npi, p.display_name, p.primary_specialty, p.practice_state, p.practice_city,
       p.practice_group_id, COALESCE(g.practice_group_size, 0),
       EXISTS (SELECT 1 FROM suppression s WHERE s.npi = p.npi)
FROM prospect p
LEFT JOIN practice_group g ON g.practice_group_id = p.practice_group_id;
"""

_DIM_CAMPAIGN = """
INSERT INTO reporting.dim_campaign (campaign, label, autonomy_level, is_active)
SELECT name, label, autonomy_level, is_active FROM campaign WHERE name <> '';
"""

_FACT_TOUCH = """
INSERT INTO reporting.fact_touch
    (message_id, npi, campaign, specialty, state, channel, variant_id, cadence_step,
     sent_at, sent_date, delivered, bounced, send_cost)
SELECT m.id, m.npi, m.campaign, p.primary_specialty, p.practice_state, m.channel, m.variant_id,
       m.cadence_step, m.sent_at,
       CASE WHEN m.sent_at IS NOT NULL THEN (m.sent_at AT TIME ZONE 'UTC')::date END,
       m.delivered, m.bounced, :send_cost
FROM message m
LEFT JOIN prospect p ON p.npi = m.npi
WHERE m.direction = 'outbound';
"""

_FACT_EVENT = """
INSERT INTO reporting.fact_event (event_id, npi, campaign, event_type, occurred_at, occurred_date)
SELECT e.id, e.npi, l.campaign, e.event_type, e.occurred_at,
       CASE WHEN e.occurred_at IS NOT NULL THEN (e.occurred_at AT TIME ZONE 'UTC')::date END
FROM event e
LEFT JOIN lead l ON l.id = e.lead_id;
"""

_FACT_FUNNEL = """
INSERT INTO reporting.fact_lead_funnel
    (lead_id, npi, campaign, specialty, state, activation_status, cadence_step, has_contact,
     sent, delivered, opened, replied, activated, is_suppressed, first_sent_at, activated_at)
SELECT l.id, l.npi, l.campaign, p.primary_specialty, p.practice_state, l.activation_status,
       l.cadence_step,
       EXISTS (SELECT 1 FROM contact c WHERE c.npi = l.npi AND c.email_status = 'valid'),
       EXISTS (SELECT 1 FROM message m WHERE m.lead_id = l.id AND m.direction = 'outbound'),
       EXISTS (SELECT 1 FROM message m WHERE m.lead_id = l.id AND m.direction = 'outbound' AND m.delivered),
       EXISTS (SELECT 1 FROM event e WHERE e.lead_id = l.id AND e.event_type = 'opened'),
       (EXISTS (SELECT 1 FROM message m WHERE m.lead_id = l.id AND m.direction = 'inbound')
        OR EXISTS (SELECT 1 FROM event e WHERE e.lead_id = l.id AND e.event_type = 'replied')),
       (l.activation_status = 'physician_activated'),
       EXISTS (SELECT 1 FROM suppression s WHERE s.npi = l.npi),
       (SELECT MIN(m.sent_at) FROM message m WHERE m.lead_id = l.id AND m.direction = 'outbound'),
       l.activation_detected_at
FROM lead l
LEFT JOIN prospect p ON p.npi = l.npi;
"""

_META = """
INSERT INTO reporting.meta (id, rebuilt_at, as_of) VALUES (1, now(), COALESCE(:as_of, now()))
ON CONFLICT (id) DO UPDATE SET rebuilt_at = now(), as_of = EXCLUDED.as_of;
"""


@dataclass
class RebuildReport:
    clinicians: int = 0
    campaigns: int = 0
    touches: int = 0
    events: int = 0
    leads: int = 0

    def lines(self) -> list:
        return [
            f"clinicians : {self.clinicians}",
            f"campaigns  : {self.campaigns}",
            f"touches    : {self.touches}",
            f"events     : {self.events}",
            f"leads      : {self.leads}",
        ]


def rebuild(session: Session, *, as_of: Optional[datetime] = None) -> RebuildReport:
    """Fully rebuild the reporting schema from operational tables. Caller owns the transaction."""
    for stmt in _DELETES:
        session.execute(text(stmt))
    session.execute(text(_DIM_CLINICIAN))
    session.execute(text(_DIM_CAMPAIGN))
    session.execute(text(_FACT_TOUCH), {"send_cost": SEND_COST})
    session.execute(text(_FACT_EVENT))
    session.execute(text(_FACT_FUNNEL))
    session.execute(text(_META), {"as_of": as_of})

    def _count(table: str) -> int:
        return session.execute(text(f"SELECT count(*) FROM reporting.{table}")).scalar() or 0

    report = RebuildReport(
        clinicians=_count("dim_clinician"), campaigns=_count("dim_campaign"),
        touches=_count("fact_touch"), events=_count("fact_event"), leads=_count("fact_lead_funnel"),
    )
    session.flush()
    METRICS.incr("reporting_rebuild")
    emit(_LOG, "reporting_rebuild", clinicians=report.clinicians, touches=report.touches,
         events=report.events, leads=report.leads)
    return report


def run_rebuild(*, settings: Optional[Settings] = None, as_of: Optional[datetime] = None) -> RebuildReport:
    """Open a session, rebuild the reporting schema, commit."""
    from certuma.db.session import make_engine
    settings = settings or get_settings()
    engine = make_engine(settings)
    with Session(engine) as session:
        report = rebuild(session, as_of=as_of)
        session.commit()
    return report


def main() -> int:
    report = run_rebuild()
    print("=== Certuma Reach reporting rebuild ===")
    for line in report.lines():
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
