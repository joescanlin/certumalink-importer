from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, List

from .models import DoctorRecord


EXPORT_FIELDS = [
    "npi",
    "first_name",
    "middle_name",
    "last_name",
    "credential",
    "display_name",
    "primary_taxonomy_code",
    "primary_specialty",
    "practice_address_1",
    "practice_address_2",
    "practice_city",
    "practice_state",
    "practice_zip",
    "practice_phone",
    "matched_zips",
    "source",
    "source_fetched_at",
]


def export_records(
    records: Iterable[DoctorRecord],
    out_path: Path,
    output_format: str | None = None,
) -> None:
    rows = [record.to_export_row() for record in records]
    fmt = output_format or infer_format(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        write_csv(rows, out_path)
        return

    if fmt == "json":
        write_json(rows, out_path)
        return

    raise ValueError(f"unsupported output format: {fmt}")


def infer_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    return "csv"


def write_csv(rows: List[dict[str, str]], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: List[dict[str, str]], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as output:
        json.dump(rows, output, indent=2, sort_keys=True)
        output.write("\n")

