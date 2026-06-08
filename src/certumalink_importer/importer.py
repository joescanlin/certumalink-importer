from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from .models import DoctorRecord
from .nppes import NppesClient
from .normalization import normalize_result
from .zipcodes import normalize_zip_code


@dataclass
class ImportStats:
    zip_count: int = 0
    response_pages: int = 0
    source_records: int = 0
    imported_records: int = 0
    skipped_records: int = 0
    duplicate_npis: int = 0
    repeated_pages_stopped: int = 0

    def to_log_payload(self) -> dict[str, int]:
        return {
            "zip_count": self.zip_count,
            "response_pages": self.response_pages,
            "source_records": self.source_records,
            "imported_records": self.imported_records,
            "skipped_records": self.skipped_records,
            "duplicate_npis": self.duplicate_npis,
            "repeated_pages_stopped": self.repeated_pages_stopped,
        }


def import_zip_codes(
    zip_codes: Iterable[str],
    client: NppesClient | None,
    fixture_path: Path | None = None,
    stats: ImportStats | None = None,
    max_pages_per_zip: int | None = None,
) -> list[DoctorRecord]:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records_by_npi: "OrderedDict[str, DoctorRecord]" = OrderedDict()
    import_stats = stats if stats is not None else ImportStats()

    for raw_zip in zip_codes:
        zip_code = normalize_zip_code(raw_zip)
        seen_page_signatures: set[tuple[str, ...]] = set()
        if fixture_path is not None:
            responses = [_load_fixture(fixture_path)]
        else:
            if client is None:
                raise ValueError("client is required when fixture_path is not provided")
            responses = client.iter_zip_search(zip_code)

        for response in _limit_pages(responses, max_pages_per_zip):
            import_stats.response_pages += 1
            results = response.get("results", [])
            if not isinstance(results, list):
                continue
            page_signature = _page_signature(results)
            if page_signature and page_signature in seen_page_signatures:
                import_stats.repeated_pages_stopped += 1
                break
            if page_signature:
                seen_page_signatures.add(page_signature)
            import_stats.source_records += len(results)
            for result in results:
                if not isinstance(result, Mapping):
                    import_stats.skipped_records += 1
                    continue
                record = normalize_result(result, matched_zip=zip_code, fetched_at=fetched_at)
                if record is None:
                    import_stats.skipped_records += 1
                    continue
                existing = records_by_npi.get(record.npi)
                if existing is None:
                    records_by_npi[record.npi] = record
                    import_stats.imported_records += 1
                else:
                    existing.add_matched_zip(zip_code)
                    import_stats.duplicate_npis += 1

    return list(records_by_npi.values())


def _limit_pages(
    responses: Iterable[Mapping[str, object]],
    max_pages: int | None,
) -> Iterator[Mapping[str, object]]:
    if max_pages is None:
        yield from responses
        return
    yield from islice(responses, max_pages)


def _load_fixture(path: Path) -> Mapping[str, object]:
    with path.open("r", encoding="utf-8") as fixture:
        payload = json.load(fixture)
    if not isinstance(payload, Mapping):
        raise ValueError(f"fixture must contain a JSON object: {path}")
    return payload


def _page_signature(results: list[object]) -> tuple[str, ...]:
    numbers: list[str] = []
    for result in results:
        if isinstance(result, Mapping):
            numbers.append(str(result.get("number") or "").strip())
    return tuple(numbers)
