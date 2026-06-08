from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .exporters import EXPORT_FIELDS, infer_format


NPI_RE = re.compile(r"^\d{10}$")
ZIP_RE = re.compile(r"^\d{5}$")


@dataclass
class ValidationReport:
    path: Path
    total_rows: int
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.is_valid:
            return f"Valid export: {self.total_rows} rows in {self.path}"
        details = "\n".join(f"- {error}" for error in self.errors)
        return f"Invalid export: {self.total_rows} rows in {self.path}\n{details}"


def validate_export(path: Path) -> ValidationReport:
    rows = _read_rows(path)
    errors: list[str] = []
    seen_npis: set[str] = set()

    for index, row in enumerate(rows, start=2):
        missing = [field for field in EXPORT_FIELDS if field not in row]
        if missing:
            errors.append(f"row {index}: missing fields {', '.join(missing)}")
            continue

        npi = str(row.get("npi", "")).strip()
        if not NPI_RE.match(npi):
            errors.append(f"row {index}: invalid NPI {npi!r}")
        elif npi in seen_npis:
            errors.append(f"row {index}: duplicate NPI {npi}")
        else:
            seen_npis.add(npi)

        practice_zip = str(row.get("practice_zip", "")).strip()
        if practice_zip and not ZIP_RE.match(practice_zip):
            errors.append(f"row {index}: invalid practice ZIP {practice_zip!r}")

        if not str(row.get("last_name", "")).strip():
            errors.append(f"row {index}: missing last_name")

        if not str(row.get("primary_taxonomy_code", "")).strip():
            errors.append(f"row {index}: missing primary_taxonomy_code")

    return ValidationReport(path=path, total_rows=len(rows), errors=errors)


def _read_rows(path: Path) -> list[dict[str, object]]:
    fmt = infer_format(path)
    if fmt == "json":
        with path.open("r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
        if not isinstance(payload, list):
            raise ValueError("JSON export must contain a list of records")
        return [row for row in payload if isinstance(row, dict)]

    with path.open("r", newline="", encoding="utf-8") as input_file:
        return list(csv.DictReader(input_file))

