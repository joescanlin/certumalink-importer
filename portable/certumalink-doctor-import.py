#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CMS_API_URL = "https://npiregistry.cms.hhs.gov/api/"
SOURCE = "cms_nppes_registry_api"
PHYSICIAN_TAXONOMY_PREFIXES = ("207", "208")
ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?$")
NPI_RE = re.compile(r"^\d{10}$")
ZIP_HEADERS = {"zip", "zipcode", "zip_code", "postal_code", "postalcode"}
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
PROMPT_FOR_ZIP = "__PROMPT_FOR_ZIP__"


@dataclass
class DoctorRecord:
    npi: str
    first_name: str
    middle_name: str
    last_name: str
    credential: str
    display_name: str
    primary_taxonomy_code: str
    primary_specialty: str
    practice_address_1: str
    practice_address_2: str
    practice_city: str
    practice_state: str
    practice_zip: str
    practice_phone: str
    source_fetched_at: str
    matched_zips: list[str] = field(default_factory=list)
    source: str = SOURCE

    def add_matched_zip(self, zip_code: str) -> None:
        if zip_code not in self.matched_zips:
            self.matched_zips.append(zip_code)

    def to_export_row(self) -> dict[str, str]:
        return {
            "npi": self.npi,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "last_name": self.last_name,
            "credential": self.credential,
            "display_name": self.display_name,
            "primary_taxonomy_code": self.primary_taxonomy_code,
            "primary_specialty": self.primary_specialty,
            "practice_address_1": self.practice_address_1,
            "practice_address_2": self.practice_address_2,
            "practice_city": self.practice_city,
            "practice_state": self.practice_state,
            "practice_zip": self.practice_zip,
            "practice_phone": self.practice_phone,
            "matched_zips": ",".join(self.matched_zips),
            "source": self.source,
            "source_fetched_at": self.source_fetched_at,
        }


@dataclass
class ImportStats:
    zip_count: int = 0
    response_pages: int = 0
    source_records: int = 0
    imported_records: int = 0
    skipped_records: int = 0
    duplicate_npis: int = 0

    def to_log_payload(self) -> dict[str, int]:
        return {
            "zip_count": self.zip_count,
            "response_pages": self.response_pages,
            "source_records": self.source_records,
            "imported_records": self.imported_records,
            "skipped_records": self.skipped_records,
            "duplicate_npis": self.duplicate_npis,
        }


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


class NppesClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        page_limit: int = 200,
        max_retries: int = 3,
        sleep_seconds: float = 0.5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.page_limit = page_limit
        self.max_retries = max_retries
        self.sleep_seconds = sleep_seconds

    def iter_zip_search(self, zip_code: str) -> Iterator[Mapping[str, object]]:
        skip = 0
        while True:
            response = self.search_zip(zip_code, skip=skip)
            yield response
            results = response.get("results", [])
            if not isinstance(results, list) or len(results) < self.page_limit:
                break
            skip += self.page_limit

    def search_zip(self, zip_code: str, *, skip: int = 0) -> Mapping[str, object]:
        params = {
            "version": "2.1",
            "enumeration_type": "NPI-1",
            "country_code": "US",
            "postal_code": zip_code,
            "limit": str(self.page_limit),
            "skip": str(skip),
        }
        url = f"{CMS_API_URL}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "certumalink-doctor-import/0.1"})
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("CMS NPPES API returned a non-object response")
                return payload
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                time.sleep(self.sleep_seconds * (2**attempt))

        raise RuntimeError(f"CMS NPPES API request failed for ZIP {zip_code}: {last_error}")


def import_zip_codes(
    zip_codes: Iterable[str],
    *,
    client: NppesClient,
    fixture_path: Path | None = None,
    max_pages_per_zip: int | None = None,
    stats: ImportStats,
    progress: Callable[[str], None] | None = None,
) -> list[DoctorRecord]:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records_by_npi: "OrderedDict[str, DoctorRecord]" = OrderedDict()

    for zip_index, raw_zip in enumerate(zip_codes, start=1):
        zip_code = normalize_zip_code(raw_zip)
        _progress(progress, f"[{zip_index}/{stats.zip_count}] Starting ZIP {zip_code}")
        responses: Iterable[Mapping[str, object]]
        if fixture_path is None:
            responses = client.iter_zip_search(zip_code)
        else:
            _progress(progress, f"[{zip_index}/{stats.zip_count}] Loading fixture data for {zip_code}")
            responses = [_load_fixture(fixture_path)]

        for page_index, response in enumerate(_limit_pages(responses, max_pages_per_zip), start=1):
            stats.response_pages += 1
            results = response.get("results", [])
            if not isinstance(results, list):
                _progress(progress, f"[{zip_code}] Page {page_index}: skipped malformed response")
                continue
            stats.source_records += len(results)
            before_imported = stats.imported_records
            before_skipped = stats.skipped_records
            before_duplicates = stats.duplicate_npis
            for result in results:
                if not isinstance(result, Mapping):
                    stats.skipped_records += 1
                    continue
                record = normalize_result(result, matched_zip=zip_code, fetched_at=fetched_at)
                if record is None:
                    stats.skipped_records += 1
                    continue
                existing = records_by_npi.get(record.npi)
                if existing is None:
                    records_by_npi[record.npi] = record
                    stats.imported_records += 1
                else:
                    existing.add_matched_zip(zip_code)
                    stats.duplicate_npis += 1
            _progress(
                progress,
                (
                    f"[{zip_code}] Page {page_index}: scanned {len(results)} CMS records, "
                    f"added {stats.imported_records - before_imported}, "
                    f"skipped {stats.skipped_records - before_skipped}, "
                    f"duplicates {stats.duplicate_npis - before_duplicates}"
                ),
            )
        _progress(
            progress,
            f"[{zip_index}/{stats.zip_count}] Finished ZIP {zip_code}: {stats.imported_records} physicians so far",
        )

    return list(records_by_npi.values())


def normalize_result(
    result: Mapping[str, object],
    *,
    matched_zip: str,
    fetched_at: str,
) -> DoctorRecord | None:
    if str(result.get("enumeration_type") or "").upper() not in ("", "NPI-1"):
        return None

    basic = _mapping(result.get("basic"))
    if not _is_active(basic):
        return None

    taxonomy = _select_physician_taxonomy(result.get("taxonomies"))
    if taxonomy is None:
        return None

    query_zip = normalize_zip_code(matched_zip)
    address = _select_practice_address(result.get("addresses"), matched_zip=query_zip)
    if address is None:
        return None

    npi = _clean(result.get("number"))
    if not npi:
        return None

    first_name = _clean(basic.get("first_name"))
    middle_name = _clean(basic.get("middle_name"))
    last_name = _clean(basic.get("last_name"))
    credential = _clean(basic.get("credential"))
    display_name = _display_name(first_name, middle_name, last_name, credential)
    record = DoctorRecord(
        npi=npi,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        credential=credential,
        display_name=display_name,
        primary_taxonomy_code=_clean(taxonomy.get("code")),
        primary_specialty=_clean(taxonomy.get("desc")),
        practice_address_1=_clean(address.get("address_1")),
        practice_address_2=_clean(address.get("address_2")),
        practice_city=_clean(address.get("city")),
        practice_state=_clean(address.get("state")),
        practice_zip=_normalize_address_zip(address.get("postal_code")),
        practice_phone=_clean(address.get("telephone_number")),
        matched_zips=[],
        source_fetched_at=fetched_at,
    )
    record.add_matched_zip(query_zip)
    return record


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


def export_records(
    records: Iterable[DoctorRecord],
    out_path: Path,
    output_format: str | None = None,
) -> None:
    rows = [record.to_export_row() for record in records]
    fmt = output_format or infer_format(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return

    if fmt == "json":
        with out_path.open("w", encoding="utf-8") as output:
            json.dump(rows, output, indent=2, sort_keys=True)
            output.write("\n")
        return

    raise ValueError(f"unsupported output format: {fmt}")


def validate_export(path: Path) -> ValidationReport:
    rows = _read_export_rows(path)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import public CMS NPPES physician records by ZIP code.",
    )
    parser.add_argument("--zip-file", help="CSV/TXT file with target ZIP codes.")
    parser.add_argument(
        "--zip",
        dest="zip_code",
        nargs="?",
        const=PROMPT_FOR_ZIP,
        help="One 5-digit ZIP code. If no value is provided, prompts for one.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV or JSON path. Defaults to a timestamped CSV in the current directory.",
    )
    parser.add_argument("--format", choices=("csv", "json"), default=None)
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum CMS pages per ZIP.")
    parser.add_argument("--fixture", default=None, help="Read CMS-like JSON fixture instead of live API.")
    parser.add_argument("--validate-only", action="store_true", help="Only validate --out.")
    parser.add_argument(
        "--json-log",
        action="store_true",
        help="Print machine-readable JSON import summary to stderr.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages. The final report still prints.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.validate_only:
            if not args.out:
                parser.error("--out is required with --validate-only")
            out_path = Path(args.out)
            report = validate_export(out_path)
            print(report.summary())
            return 0 if report.is_valid else 1

        if args.zip_file:
            zip_codes = read_zip_file(Path(args.zip_file))
        elif args.zip_code is not None:
            zip_value = _prompt_for_zip() if args.zip_code == PROMPT_FOR_ZIP else args.zip_code
            zip_codes = [normalize_zip_code(zip_value)]
        else:
            parser.error("provide either --zip-file or --zip")

        if not zip_codes:
            raise ValueError("no valid ZIP codes found")

        max_pages = _max_pages(args.max_pages)
        out_path = Path(args.out) if args.out else _default_out_path(zip_codes, args.format)
        progress = None if args.quiet else _print_progress
        _progress(progress, "Preparing CMS NPPES physician import")
        _progress(progress, f"ZIPs queued: {len(zip_codes)}")
        if max_pages is not None:
            _progress(progress, f"Page limit: {max_pages} page(s) per ZIP")
        _progress(progress, f"Output will be written to: {out_path}")
        stats = ImportStats(zip_count=len(zip_codes))
        records = import_zip_codes(
            zip_codes,
            client=NppesClient(),
            fixture_path=Path(args.fixture) if args.fixture else None,
            max_pages_per_zip=max_pages,
            stats=stats,
            progress=progress,
        )
        _progress(progress, f"Writing {len(records)} physician records to {out_path}")
        export_records(records, out_path, output_format=args.format)
        _progress(progress, "Validating export file")
        report = validate_export(out_path)
        _progress(progress, "Import complete")

        if args.json_log:
            payload = stats.to_log_payload()
            payload["event"] = "import_summary"
            payload["exported_records"] = len(records)
            payload["out_path"] = str(out_path)
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        _print_report(
            stats=stats,
            exported_records=len(records),
            out_path=out_path,
            zip_codes=zip_codes,
            report=report,
        )
        return 0 if report.is_valid else 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def infer_format(path: Path) -> str:
    return "json" if path.suffix.lower() == ".json" else "csv"


def _prompt_for_zip() -> str:
    try:
        return input("Enter ZIP code: ").strip()
    except EOFError as exc:
        raise ValueError("ZIP code is required") from exc


def _default_out_path(zip_codes: list[str], output_format: str | None) -> Path:
    extension = "json" if output_format == "json" else "csv"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if len(zip_codes) == 1:
        name = f"certumalink-doctors-{zip_codes[0]}-{stamp}.{extension}"
    else:
        name = f"certumalink-doctors-{len(zip_codes)}-zips-{stamp}.{extension}"
    return Path.cwd() / name


def _print_report(
    *,
    stats: ImportStats,
    exported_records: int,
    out_path: Path,
    zip_codes: list[str],
    report: ValidationReport,
) -> None:
    shown_zips = ", ".join(zip_codes[:8])
    if len(zip_codes) > 8:
        shown_zips += f", ... ({len(zip_codes)} total)"

    print()
    print("Certumalink Doctor Import Report")
    print("--------------------------------")
    print(f"ZIPs: {shown_zips}")
    print(f"Output: {out_path}")
    print(f"CMS records scanned: {stats.source_records}")
    print(f"Physicians exported: {exported_records}")
    print(f"Skipped records: {stats.skipped_records}")
    print(f"Duplicate NPIs merged: {stats.duplicate_npis}")
    print(f"CMS response pages: {stats.response_pages}")
    print(f"Validation: {'passed' if report.is_valid else 'failed'}")
    if not report.is_valid:
        print(report.summary())
    print()
    print("Review note: NPPES records are public provider-reported data and need review before publishing.")


def _print_progress(message: str) -> None:
    print(f"[certumalink] {message}", flush=True)


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _read_export_rows(path: Path) -> list[dict[str, object]]:
    if infer_format(path) == "json":
        with path.open("r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
        if not isinstance(payload, list):
            raise ValueError("JSON export must contain a list of records")
        return [row for row in payload if isinstance(row, dict)]

    with path.open("r", newline="", encoding="utf-8") as input_file:
        return list(csv.DictReader(input_file))


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


def _is_active(basic: Mapping[str, object]) -> bool:
    status = _clean(basic.get("status")).upper()
    if status and status != "A":
        return False
    if _clean(basic.get("deactivation_date")):
        return False
    if _clean(basic.get("deactivation_reason_code")):
        return False
    return True


def _select_physician_taxonomy(value: object) -> Mapping[str, object] | None:
    taxonomies = [_mapping(item) for item in _list(value)]
    physician_taxonomies = [
        taxonomy for taxonomy in taxonomies if _clean(taxonomy.get("code")).startswith(PHYSICIAN_TAXONOMY_PREFIXES)
    ]
    if not physician_taxonomies:
        return None
    for taxonomy in physician_taxonomies:
        if _is_truthy(taxonomy.get("primary")):
            return taxonomy
    return physician_taxonomies[0]


def _select_practice_address(value: object, *, matched_zip: str) -> Mapping[str, object] | None:
    addresses = [_mapping(item) for item in _list(value)]
    candidates = [
        address
        for address in addresses
        if _clean(address.get("country_code")).upper() in ("", "US")
    ] or addresses
    for address in candidates:
        if (
            _clean(address.get("address_purpose")).upper() == "LOCATION"
            and _normalize_address_zip(address.get("postal_code")) == matched_zip
        ):
            return address
    return None


def _display_name(first: str, middle: str, last: str, credential: str) -> str:
    display = " ".join(part for part in (first, middle, last) if part)
    return f"{display}, {credential}" if display and credential else display or credential


def _normalize_address_zip(value: object) -> str:
    text = _clean(value)
    return text[:5] if len(text) >= 5 and text[:5].isdigit() else text


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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _max_pages(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise ValueError("--max-pages must be 1 or greater")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
