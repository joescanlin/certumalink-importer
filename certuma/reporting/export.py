"""Series-A evidence export (Phase 3 task P3.2).

Exports the GOVERNED, aggregate Customer-Intelligence datasets (conversion by specialty / region /
campaign, funnel totals, unit economics, a suppression-governance summary) for the data room. The
datasets are aggregates only - no row-level PII, suppressed clinicians excluded from every metric -
so the export honors opt-out by construction (latent issue 2).

Output goes through an Exporter seam: CsvExporter writes local CSV now; a parquet writer or an
external-warehouse exporter (Snowflake/BigQuery) slots in behind the same interface later (the
warehouse-target decision). Run on a freshly rebuilt reporting schema (elt.rebuild / `make rebuild`).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from certuma.config import Settings, get_settings
from certuma.observability import emit, get_logger
from certuma.reporting import queries as rq

__all__ = ["Exporter", "CsvExporter", "MemoryExporter", "ExportReport", "export_evidence",
           "run_export", "main"]

_LOG = get_logger("certuma.reporting.export")


class Exporter(Protocol):
    def export(self, name: str, rows: List[dict]) -> None:
        ...


class MemoryExporter:
    """Captures datasets in memory (tests / inspection)."""

    def __init__(self) -> None:
        self.tables: Dict[str, List[dict]] = {}

    def export(self, name: str, rows: List[dict]) -> None:
        self.tables[name] = rows


class CsvExporter:
    """Writes each dataset as <out_dir>/<name>.csv."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def export(self, name: str, rows: List[dict]) -> None:
        path = self.out_dir / f"{name}.csv"
        if not rows:
            path.write_text("")
            return
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


@dataclass
class ExportReport:
    tables: List[str] = field(default_factory=list)
    rows: int = 0


def export_evidence(session: Session, *, exporter: Exporter) -> ExportReport:
    """Write the governed aggregate datasets through the exporter. Read-only on operational state."""
    gov = session.execute(text(
        "SELECT count(*) AS clinicians, count(*) FILTER (WHERE is_suppressed) AS suppressed "
        "FROM reporting.dim_clinician")).mappings().one()

    datasets = {
        "funnel_totals": [rq.funnel_totals(session)],
        "conversion_by_specialty": rq.by_dimension(session, "specialty"),
        "conversion_by_campaign": rq.by_dimension(session, "campaign"),
        "conversion_by_region": rq.by_dimension(session, "state"),
        "unit_economics": [rq.unit_economics(session)],
        "governance_summary": [dict(gov)],
    }
    report = ExportReport()
    for name, rows in datasets.items():
        exporter.export(name, rows)
        report.tables.append(name)
        report.rows += len(rows)
    emit(_LOG, "evidence_exported", tables=len(report.tables), suppressed_excluded=gov["suppressed"])
    return report


def run_export(*, settings: Settings = None, out_dir: str = "") -> ExportReport:
    """Open a session and export the evidence datasets as CSV. Read-only."""
    from certuma.db.session import make_engine
    settings = settings or get_settings()
    out_dir = out_dir or "evidence"
    engine = make_engine(settings)
    with Session(engine) as session:
        return export_evidence(session, exporter=CsvExporter(out_dir))


def main() -> int:
    import os
    out_dir = os.environ.get("CERTUMA_EVIDENCE_DIR", "evidence")
    report = run_export(out_dir=out_dir)
    print("=== Certuma Reach evidence export ===")
    print(f"  wrote {len(report.tables)} datasets to ./{out_dir}/:")
    for t in report.tables:
        print(f"    - {t}.csv")
    print("  (governed aggregates only; suppressed clinicians excluded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
