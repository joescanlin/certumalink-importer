"""Claim-status poller (Phase 1 task P1.10).

The platform activation webhook does not exist yet (see certuma.publish.claim_status), so until it
does we POLL each sent lead's claim_url. A click/claim is THE conversion signal (decision #6), and
the poller is one of only two actors permitted to set physician_activated
(certuma_core.status.ACTIVATION_ONLY_ACTORS).

poll_once selects every still-open lead that has a claim_url, asks the injected `fetch` for its
status, and on a claimed result records a deduped 'activated' event and drives the lead
email_sent|awaiting_reply -> interested -> physician_activated (actor='poller') via the shared
monitor.activate_lead. last_polled_at is stamped on every lead each pass so polling cadence is
observable. The default fetch still raises ClaimStatusUnavailable, so wiring the poller without a
real claim-status source fails loudly rather than silently reporting no activations.

Dedup: the per-lead event key is 'claim:<npi>:<campaign>', so a lead already activated in a prior
pass is a no-op (record_event returns None) and activate_lead short-circuits on physician_activated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma import monitor
from certuma.db.models import Lead
from certuma.observability import METRICS, emit, get_logger
from certuma.publish.claim_status import ClaimStatusUnavailable, default_fetch

__all__ = ["PollSummary", "poll_once", "CLAIMED_STATUSES", "POLLABLE_STATES"]

_LOG = get_logger("certuma.poller")

# Lead states from which a claim click can still convert (sent, not yet terminal).
POLLABLE_STATES = ("email_sent", "awaiting_reply", "interested")

# Statuses the claim-status source uses to mean "the physician claimed the profile".
CLAIMED_STATUSES = frozenset({"claimed", "activated", "active"})


@dataclass
class PollSummary:
    polled: int = 0
    activated: int = 0
    errors: int = 0
    activated_npis: List[str] = field(default_factory=list)


def poll_once(
    session: Session,
    *,
    fetch: Callable[[str], str] = default_fetch,
    when: Optional[datetime] = None,
    limit: int = 500,
) -> PollSummary:
    """Poll every open lead with a claim_url once. Caller owns the transaction (commit on success)."""
    when = when or datetime.now(timezone.utc)
    leads = session.execute(
        select(Lead).where(
            Lead.claim_url.isnot(None), Lead.claim_url != "",
            Lead.activation_status.in_(POLLABLE_STATES),
        ).order_by(Lead.id).limit(limit)
    ).scalars().all()

    summary = PollSummary()
    for lead in leads:
        lead.last_polled_at = when
        summary.polled += 1
        try:
            status = fetch(lead.claim_url)
        except ClaimStatusUnavailable:
            raise  # no source wired: fail loudly, do not mark everything un-activated
        except Exception as exc:  # a per-lead fetch failure must not abort the whole pass
            summary.errors += 1
            METRICS.incr("poll_fetch_error")
            emit(_LOG, "poll_fetch_error", lead_id=lead.id, error=str(exc))
            continue
        if status not in CLAIMED_STATUSES:
            continue
        # Idempotent activation: dedup the click, then drive the conversion as the poller.
        event_id = monitor.record_event(
            session, event_type="activated", dedup_key=f"claim:{lead.npi}:{lead.campaign}",
            occurred_at=when, lead_id=lead.id, npi=lead.npi, payload={"status": status},
        )
        if event_id is None:
            continue  # already recorded in a prior pass
        if monitor.activate_lead(session, lead, actor="poller", when=when):
            summary.activated += 1
            summary.activated_npis.append(lead.npi)

    session.flush()
    METRICS.incr("poll_run")
    emit(_LOG, "poll_run", polled=summary.polled, activated=summary.activated, errors=summary.errors)
    return summary
