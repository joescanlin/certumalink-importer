"""The scheduler tick (Phase 2 task P2.7).

One deterministic pass over the whole loop, in dependency order, each step idempotent:

  1. propose            draft a pending Approval for every sendable lead on an active campaign
  2. auto_execute       send the proposals the autonomy policy clears (escalate the rest)
  3. cadence            send the next due follow-up for awaiting_reply / interested leads
  4. poll               convert a claim-click to physician_activated (only if a claim source is wired)
  5. expire_sla         flip pending proposals past their SLA to expired

A cron/worker calls tick() on a schedule; tick() itself is a pure function of (now, providers) so it
is fully testable. Inbound events (delivered, bounce, opt-out, replies) arrive out-of-band through
the monitor / inbound webhooks, not here - the tick only drives the OUTBOUND + activation side
forward. Enrichment (P2.5) will slot in as step 0 when it lands. Caller owns the transaction;
run_once opens one, ticks, and commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from certuma import cadence, orchestrator, poller
from certuma.config import Settings, get_settings
from certuma.observability import METRICS, emit, get_logger

__all__ = ["TickReport", "tick", "run_once", "main"]

_LOG = get_logger("certuma.scheduler")


@dataclass
class TickReport:
    proposed: int = 0
    auto_sent: int = 0
    escalated: int = 0
    cadence_sent: int = 0
    cadence_exhausted: int = 0
    activated: int = 0
    expired: int = 0

    def lines(self) -> list:
        return [
            f"proposed     : {self.proposed}",
            f"auto-sent    : {self.auto_sent}   escalated to human: {self.escalated}",
            f"cadence sent : {self.cadence_sent}   exhausted: {self.cadence_exhausted}",
            f"activated    : {self.activated}",
            f"expired (SLA): {self.expired}",
        ]


def tick(
    session: Session,
    *,
    copy_provider,
    email_provider,
    settings: Optional[Settings] = None,
    when: Optional[datetime] = None,
    claim_fetch: Optional[Callable[[str], str]] = None,
) -> TickReport:
    """Run one full pass of the autonomous loop. Caller owns the transaction."""
    settings = settings or get_settings()
    when = when or datetime.now(timezone.utc)
    report = TickReport()

    p = orchestrator.propose_sends(session, provider=copy_provider, settings=settings, when=when)
    report.proposed = p.proposed

    a = orchestrator.auto_execute_pending(session, provider_email=email_provider, settings=settings, when=when)
    report.auto_sent, report.escalated = a.auto_sent, a.escalated

    c = cadence.run_cadence(session, copy_provider=copy_provider, email_provider=email_provider,
                            settings=settings, when=when)
    report.cadence_sent, report.cadence_exhausted = c.sent, c.exhausted

    if claim_fetch is not None:  # only poll when a claim-status source is wired
        report.activated = poller.poll_once(session, fetch=claim_fetch, when=when).activated

    report.expired = orchestrator.expire_stale_approvals(session, when=when)

    METRICS.incr("scheduler_tick")
    emit(_LOG, "scheduler_tick", proposed=report.proposed, auto_sent=report.auto_sent,
         cadence_sent=report.cadence_sent, activated=report.activated)
    return report


def run_once(
    *,
    settings: Optional[Settings] = None,
    copy_provider=None,
    email_provider=None,
    claim_fetch: Optional[Callable[[str], str]] = None,
    when: Optional[datetime] = None,
) -> TickReport:
    """Open a session, run one tick, commit. Builds dev providers (stub copy + Mailpit) by default."""
    from certuma.copywriter import StubCopyProvider
    from certuma.db.session import make_engine
    from certuma.email import get_provider

    settings = settings or get_settings()
    copy_provider = copy_provider or StubCopyProvider()
    email_provider = email_provider or get_provider(settings)
    engine = make_engine(settings)
    with Session(engine) as session:
        report = tick(session, copy_provider=copy_provider, email_provider=email_provider,
                      settings=settings, when=when, claim_fetch=claim_fetch)
        session.commit()
    return report


def main() -> int:
    report = run_once()
    print("=== Certuma Reach scheduler tick ===")
    for line in report.lines():
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
