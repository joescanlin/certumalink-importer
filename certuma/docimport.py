"""Import a generated certumalink_run document into the Reach platform.

Reads the physician rows of a document in the terminal's workspace (a doctors.csv / profile-drafts
style CSV with an `npi` column), upserts each as a Prospect, and - when a campaign is given - ensures
a Lead so the imported doctors enter the pipeline. The path is validated through webterm.safe_doc_path
so only files inside the workspace can be read. Caller commits.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from certuma import webterm
from certuma.db.models import Campaign, Lead, Prospect
from certuma.observability import METRICS, emit, get_logger

__all__ = ["ImportResult", "import_document"]

_LOG = get_logger("certuma.docimport")
_NPI_RE = re.compile(r"^\d{10}$")
# columns copied straight onto Prospect when present (npi is the key; matched_zips/source are derived)
_TEXT_FIELDS = ("first_name", "middle_name", "last_name", "credential", "display_name",
                "primary_taxonomy_code", "primary_specialty", "practice_address_1",
                "practice_address_2", "practice_city", "practice_zip", "practice_phone")


@dataclass
class ImportResult:
    total: int = 0
    created: int = 0
    updated: int = 0
    leads_created: int = 0
    skipped: int = 0


def import_document(session: Session, rel: str, *, campaign=None) -> ImportResult:
    """Upsert the physician rows of a workspace document. With a campaign, also ensures a Lead."""
    path = webterm.safe_doc_path(rel)
    if path.suffix.lower() != ".csv":
        raise ValueError("only CSV documents can be imported")
    if campaign is not None and session.get(Campaign, campaign) is None:
        raise ValueError(f"unknown campaign {campaign!r}")

    result = ImportResult()
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "npi" not in reader.fieldnames:
            raise ValueError("this document has no npi column to import")
        for row in reader:
            result.total += 1
            npi = (row.get("npi") or "").strip()
            if not _NPI_RE.match(npi):
                result.skipped += 1
                continue
            prospect = session.get(Prospect, npi)
            if prospect is None:
                prospect = Prospect(npi=npi)
                session.add(prospect)
                result.created += 1
            else:
                result.updated += 1
            for field in _TEXT_FIELDS:
                val = (row.get(field) or "").strip()
                if val:
                    setattr(prospect, field, val)
            state = (row.get("practice_state") or "").strip()[:2]  # column is CHAR(2)
            if state:
                prospect.practice_state = state
            if not (prospect.display_name or "").strip():
                # first/last may still be None pre-flush (the column default is applied on insert)
                name = f"{(prospect.first_name or '').strip()} {(prospect.last_name or '').strip()}".strip()
                prospect.display_name = name or npi
            session.flush()
            if campaign is not None:
                exists = session.execute(
                    select(Lead.id).where(Lead.npi == npi, Lead.campaign == campaign).limit(1)).first()
                if exists is None:
                    session.add(Lead(npi=npi, campaign=campaign, activation_status="not_contacted"))
                    result.leads_created += 1
    session.flush()
    METRICS.incr("document_imported", created=str(result.created), updated=str(result.updated))
    emit(_LOG, "document_imported", rel=rel, campaign=campaign, created=result.created,
         updated=result.updated, leads=result.leads_created, skipped=result.skipped)
    return result
