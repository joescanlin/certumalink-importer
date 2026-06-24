"""Signal collection node (Phase 3 task P3.3).

collect_signals upserts the signals a set of providers return for one clinician (one current value
per npi/type/source). run_signal_collection is the batch entry: it collects signals for prospects
that have a lead but no signals yet (the dev/loop default uses the public + vendor-stub providers).
The stored signals feed trigger-signal scoring + Recommended Actions (P3.4). Caller owns the txn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from certuma.db.models import ClinicianSignal, Prospect
from certuma.observability import METRICS, emit, get_logger

from .provider import ClinicianFacts, Signal
from .stub import PublicSignalProvider, VendorSignalProvider

__all__ = ["SignalSummary", "facts_for", "collect_signals", "run_signal_collection", "default_providers"]

_LOG = get_logger("certuma.signals")


def default_providers():
    """The dev/loop default: public signals + the vendor stub (real vendor slots in here later)."""
    return [PublicSignalProvider(), VendorSignalProvider()]


@dataclass
class SignalSummary:
    clinicians: int = 0
    signals_written: int = 0
    npis: List[str] = field(default_factory=list)


def facts_for(session: Session, prospect: Prospect) -> ClinicianFacts:
    group_size = 0
    if prospect.practice_group_id:
        group_size = session.execute(text(
            "SELECT practice_group_size FROM practice_group WHERE practice_group_id = :g"),
            {"g": prospect.practice_group_id}).scalar() or 0
    return ClinicianFacts(
        npi=prospect.npi, first_name=prospect.first_name or "", last_name=prospect.last_name or "",
        specialty=prospect.primary_specialty or "", state=prospect.practice_state or "",
        city=prospect.practice_city or "", group_size=int(group_size),
    )


def _upsert(session: Session, *, npi: str, signal: Signal, observed_at: datetime) -> None:
    row = session.execute(
        select(ClinicianSignal).where(
            ClinicianSignal.npi == npi, ClinicianSignal.signal_type == signal.signal_type,
            ClinicianSignal.source == signal.source)
    ).scalar()
    if row is None:
        session.add(ClinicianSignal(
            npi=npi, signal_type=signal.signal_type, value=signal.value,
            numeric_value=signal.numeric_value, source=signal.source,
            confidence=signal.confidence, observed_at=observed_at))
    else:
        row.value = signal.value
        row.numeric_value = signal.numeric_value
        row.confidence = signal.confidence
        row.observed_at = observed_at


def collect_signals(
    session: Session, *, facts: ClinicianFacts, providers: Sequence, when: Optional[datetime] = None
) -> int:
    """Upsert all signals the providers return for one clinician. Returns the count written."""
    when = when or datetime.now(timezone.utc)
    n = 0
    for provider in providers:
        for signal in provider.signals(facts):
            _upsert(session, npi=facts.npi, signal=signal, observed_at=when)
            n += 1
    session.flush()
    return n


def run_signal_collection(
    session: Session, *, providers: Optional[Sequence] = None, when: Optional[datetime] = None,
    limit: int = 200,
) -> SignalSummary:
    """Collect signals for prospects that have a lead but no signals yet. Caller commits."""
    providers = providers or default_providers()
    when = when or datetime.now(timezone.utc)
    prospects = session.execute(
        select(Prospect).where(
            text("EXISTS (SELECT 1 FROM lead l WHERE l.npi = prospect.npi)"),
            text("NOT EXISTS (SELECT 1 FROM clinician_signal s WHERE s.npi = prospect.npi)"),
        ).order_by(Prospect.npi).limit(limit)
    ).scalars().all()

    summary = SignalSummary()
    for prospect in prospects:
        written = collect_signals(session, facts=facts_for(session, prospect), providers=providers, when=when)
        summary.clinicians += 1
        summary.signals_written += written
        summary.npis.append(prospect.npi)
    session.flush()
    METRICS.incr("signal_collection_run")
    emit(_LOG, "signal_collection_run", clinicians=summary.clinicians, signals=summary.signals_written)
    return summary
