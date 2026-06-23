"""CSV -> Postgres seed migration for the legacy activation_status ledger (Phase 0 tasks B11/B12).

Reuses certuma_core.status to normalize legacy statuses and validate against the 14-state set
(unknown status hard-fails the migration, preserving the monolith's behavior). Within-file
duplicate NPIs resolve to the newest last_seen_at *instant* (timestamps are parsed to tz-aware
datetimes, so mixed offsets compare correctly) - a deliberate, documented divergence from the
monolith's last-row-wins. Every legacy lead is assigned the seeded 'legacy' campaign.

Both upsert helpers are NON-DESTRUCTIVE on conflict:
  - Lead: writes SEED columns only; activation_status is set ONLY on first insert; last_seen_at
    only ever advances (greatest). It can NEVER clobber live conversation state
    (activation_status / next_action_at / cadence_step / claim_url / version).
  - Prospect: a blank ledger value never overwrites an existing populated column (COALESCE/NULLIF),
    so a thin nightly re-run cannot blank out enrichment.

seed() does not commit; the caller owns the transaction. dry_run=True writes nothing.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from certuma_core.status import STATES, normalize_status
from certuma.db.models import Lead, Prospect
from certuma.observability import METRICS, emit, get_logger

__all__ = ["LEGACY_CAMPAIGN", "LEAD_STATE_COLUMNS", "Reconciliation", "read_ledger", "prepare", "seed"]

_LOG = get_logger("certuma.seed")

LEGACY_CAMPAIGN = "legacy"
REQUIRED_COLUMNS = {"npi", "activation_status"}
# state columns that must NEVER be touched by a re-run (the Lead clobber guard, asserted by test)
LEAD_STATE_COLUMNS = ("activation_status", "next_action_at", "cadence_step", "claim_url", "version")
# timestamps below this sort sentinel (or None) never win a dedup
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class Reconciliation:
    csv_row_count: int = 0
    empty_npi_skipped: int = 0
    npi_unique_count: int = 0
    status_histogram_raw: dict = field(default_factory=dict)
    status_histogram_normalized: dict = field(default_factory=dict)
    legacy_rewrites: int = 0
    unknown_statuses: list = field(default_factory=list)
    leads_to_insert: int = 0
    leads_to_update: int = 0
    dry_run: bool = True

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 last_seen_at to a tz-aware datetime; '' -> None; naive -> UTC.

    Raises ValueError on a present-but-unparseable value (a corrupt timestamp could pick the
    wrong winning status, which is a real safety issue, so it hard-fails like an unknown status).
    """
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"unparseable last_seen_at: {value!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def read_ledger(path: Path | str) -> list[dict]:
    """Read activation_status.csv rows verbatim. Hard-fail if required columns are absent."""
    p = Path(path)
    with p.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"activation_status.csv missing required columns: {sorted(missing)}")
        return list(reader)


def prepare(raw_rows: list[dict]) -> tuple[list[dict], Reconciliation]:
    """Normalize, legacy-map, validate, and dedup by newest last_seen_at instant.

    Does not raise on unknown status (seed() decides to abort); does raise on an unparseable
    timestamp. Dedup keeps the newest instant; on an equal instant the later CSV row wins (>=).
    """
    recon = Reconciliation()
    by_npi: dict[str, dict] = {}
    for raw in raw_rows:
        recon.csv_row_count += 1
        npi = str(raw.get("npi", "")).strip()
        if not npi:
            recon.empty_npi_skipped += 1
            continue
        raw_status = str(raw.get("activation_status", "")).strip()
        recon.status_histogram_raw[raw_status] = recon.status_histogram_raw.get(raw_status, 0) + 1
        status = normalize_status(raw_status)
        if status != raw_status and raw_status != "":
            recon.legacy_rewrites += 1
        recon.status_histogram_normalized[status] = recon.status_histogram_normalized.get(status, 0) + 1
        if status not in STATES and status not in recon.unknown_statuses:
            recon.unknown_statuses.append(status)
        row = {
            "npi": npi,
            "activation_status": status,
            "profile_url": str(raw.get("profile_url", "")).strip(),
            "display_name": str(raw.get("display_name", "")).strip(),
            "specialty": str(raw.get("specialty", "")).strip(),
            "practice_zip": str(raw.get("practice_zip", "")).strip(),
            "last_seen_at": _parse_ts(raw.get("last_seen_at", "")),
        }
        prev = by_npi.get(npi)
        if prev is None or (row["last_seen_at"] or _EPOCH) >= (prev["last_seen_at"] or _EPOCH):
            by_npi[npi] = row
    recon.npi_unique_count = len(by_npi)
    return list(by_npi.values()), recon


def _upsert_prospect_seed(session: Session, rows: list[dict]) -> None:
    """Insert/refresh prospect stubs. ON CONFLICT refreshes SEED columns but a BLANK ledger
    value never overwrites an existing populated value (so a thin re-run can't blank enrichment)."""
    for row in rows:
        values = {
            "npi": row["npi"],
            "display_name": row["display_name"],
            "primary_specialty": row["specialty"],
            "practice_zip": row["practice_zip"],
            "profile_url": row["profile_url"] or None,
        }
        stmt = pg_insert(Prospect).values(**values)
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["npi"],
            set_={
                "display_name": func.coalesce(func.nullif(ex.display_name, ""), Prospect.display_name),
                "primary_specialty": func.coalesce(func.nullif(ex.primary_specialty, ""), Prospect.primary_specialty),
                "practice_zip": func.coalesce(func.nullif(ex.practice_zip, ""), Prospect.practice_zip),
                "profile_url": func.coalesce(ex.profile_url, Prospect.profile_url),
                "updated_at": func.now(),  # "last seen by importer", not "last data change"
            },
        )
        session.execute(stmt)


def _upsert_lead_seed(session: Session, rows: list[dict]) -> None:
    """Insert leads on the legacy campaign. ON CONFLICT refreshes last_seen_at ONLY (and only
    forward, via greatest), so a re-run never clobbers live state. activation_status is set
    ONLY on first insert. The set-clause excludes every column in LEAD_STATE_COLUMNS."""
    for row in rows:
        stmt = pg_insert(Lead).values(
            npi=row["npi"],
            campaign=LEGACY_CAMPAIGN,
            activation_status=row["activation_status"],
            last_seen_at=row["last_seen_at"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["npi", "campaign"],
            set_={
                "last_seen_at": func.greatest(Lead.last_seen_at, stmt.excluded.last_seen_at),
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)


def seed(session: Session, path: Path | str, *, dry_run: bool = True) -> Reconciliation:
    """Run the legacy-ledger migration. Aborts (ValueError) on any unknown status.

    Does not commit. dry_run=True writes nothing and reports insert/update counts.
    """
    rows, recon = prepare(read_ledger(path))
    recon.dry_run = dry_run

    if recon.unknown_statuses:
        METRICS.incr("seed_abort", reason="unknown_status")
        emit(_LOG, "seed_aborted", reason="unknown_status", statuses=sorted(recon.unknown_statuses))
        raise ValueError(f"unknown activation statuses, aborting migration: {sorted(recon.unknown_statuses)}")

    npis = [r["npi"] for r in rows]
    existing: set = set()
    if npis:
        existing = set(
            session.execute(
                select(Lead.npi).where(Lead.campaign == LEGACY_CAMPAIGN, Lead.npi.in_(npis))
            ).scalars()
        )
    recon.leads_to_update = sum(1 for n in npis if n in existing)
    recon.leads_to_insert = len(npis) - recon.leads_to_update

    if not dry_run:
        _upsert_prospect_seed(session, rows)   # prospects first (lead.npi FK)
        _upsert_lead_seed(session, rows)
        session.flush()

    METRICS.incr("seed_run", dry_run=str(dry_run))
    emit(_LOG, "seed_reconciliation", dry_run=dry_run, csv_rows=recon.csv_row_count,
         unique=recon.npi_unique_count, inserts=recon.leads_to_insert,
         updates=recon.leads_to_update, legacy_rewrites=recon.legacy_rewrites,
         skipped_empty_npi=recon.empty_npi_skipped)
    return recon
