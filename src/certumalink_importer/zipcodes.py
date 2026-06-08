from __future__ import annotations

import csv
import re
from pathlib import Path


ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?$")
ZIP_HEADERS = {"zip", "zipcode", "zip_code", "postal_code", "postalcode"}


def read_zip_file(path: Path) -> list[str]:
    rows = _read_csvish_rows(path)
    if not rows:
        return []

    header_index = _zip_header_index(rows[0])
    data_rows = rows[1:] if header_index is not None else rows
    values: list[str] = []

    for row in data_rows:
        if not row:
            continue
        candidates = [row[header_index]] if header_index is not None and header_index < len(row) else row
        for candidate in candidates:
            try:
                values.append(normalize_zip_code(candidate))
            except ValueError:
                continue

    return _dedupe(values)


def normalize_zip_code(value: str) -> str:
    text = str(value).strip()
    match = ZIP_RE.match(text)
    if not match:
        raise ValueError(f"invalid US ZIP code: {value!r}")
    return match.group(1)


def _read_csvish_rows(path: Path) -> list[list[str]]:
    with path.open("r", newline="", encoding="utf-8") as input_file:
        return [[cell.strip() for cell in row] for row in csv.reader(input_file) if row]


def _zip_header_index(row: list[str]) -> int | None:
    for index, value in enumerate(row):
        if value.strip().lower() in ZIP_HEADERS:
            return index
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped

