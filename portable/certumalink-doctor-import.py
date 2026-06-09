#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
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
SCRIPT_DOWNLOAD_URL = "https://raw.githubusercontent.com/joescanlin/certumalink-importer/main/portable/certumalink-doctor-import.py"
CERTUMALINK_BASE_URL = "https://www.certumalink.com"
SOURCE = "cms_nppes_registry_api"
PHYSICIAN_TAXONOMY_PREFIXES = ("207", "208")
ZIP_RE = re.compile(r"(\d{5})(?:-\d{4})?$")
NPI_RE = re.compile(r"^\d{10}$")
ZIP_HEADERS = {"zip", "zipcode", "zip_code", "postal_code", "postalcode"}
DEFAULT_ACTIVATION_STATUS = "not_contacted"
LEGACY_ACTIVATION_STATUS_MAP = {
    "draft_profile_created": "not_contacted",
    "rox_contacted": "email_sent",
    "activated": "physician_activated",
}
VALID_ACTIVATION_STATUSES = {
    "not_contacted",
    "queued_today",
    "called_no_answer",
    "voicemail_left",
    "email_sent",
    "interested",
    "physician_activated",
    "do_not_contact",
    "needs_review",
}
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
PROFILE_DRAFT_FIELDS = [
    "npi",
    "profile_url",
    "profile_slug",
    "claim_url",
    "display_name",
    "first_name",
    "last_name",
    "credential",
    "specialty",
    "taxonomy_code",
    "city",
    "state",
    "practice_zip",
    "practice_phone",
    "source",
    "source_fetched_at",
    "campaign",
    "activation_status",
    "activation_priority",
    "activation_score",
    "priority_reason",
    "profile_completeness_score",
    "missing_profile_fields",
    "practice_group_id",
    "practice_group_size",
    "other_doctors_at_location",
]
ROX_OUTREACH_FIELDS = [
    "npi",
    "doctor_name",
    "campaign",
    "specialty",
    "practice_phone",
    "city",
    "state",
    "profile_url",
    "claim_url",
    "activation_status",
    "activation_priority",
    "activation_score",
    "priority_reason",
    "profile_completeness_score",
    "missing_profile_fields",
    "practice_group_id",
    "practice_group_size",
    "other_doctors_at_location",
    "suggested_pitch",
    "call_opener_draft",
    "voicemail_draft",
    "email_subject_draft",
    "email_body_draft",
    "follow_up_draft",
]
ROX_TODAY_FIELDS = [
    "queue_rank",
    *ROX_OUTREACH_FIELDS,
]
ACTIVATION_STATUS_FIELDS = [
    "npi",
    "activation_status",
    "profile_url",
    "display_name",
    "specialty",
    "practice_zip",
    "last_seen_at",
]
PRACTICE_GROUP_FIELDS = [
    "practice_group_id",
    "practice_group_size",
    "practice_phone",
    "practice_address_1",
    "practice_address_2",
    "practice_city",
    "practice_state",
    "practice_zip",
    "doctors",
    "npi_list",
]
PROMPT_FOR_ZIP = "__PROMPT_FOR_ZIP__"
QUEUE_EXCLUDED_STATUSES = {"physician_activated", "do_not_contact", "needs_review"}


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
    repeated_pages_stopped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def add_skip(self, reason: str, count: int = 1) -> None:
        self.skipped_records += count
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + count

    def to_log_payload(self) -> dict[str, object]:
        return {
            "zip_count": self.zip_count,
            "response_pages": self.response_pages,
            "source_records": self.source_records,
            "imported_records": self.imported_records,
            "skipped_records": self.skipped_records,
            "duplicate_npis": self.duplicate_npis,
            "repeated_pages_stopped": self.repeated_pages_stopped,
            "skip_reasons": self.skip_reasons,
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


@dataclass
class OutputBundle:
    bundle_mode: bool
    output_dir: Path
    doctors_path: Path
    profile_drafts_path: Path | None = None
    rox_outreach_path: Path | None = None
    rox_today_path: Path | None = None
    practice_groups_path: Path | None = None
    publish_payload_path: Path | None = None
    publish_result_path: Path | None = None
    activation_status_path: Path | None = None
    summary_path: Path | None = None


@dataclass(frozen=True)
class CampaignPreset:
    name: str
    label: str
    specialty_terms: tuple[str, ...]
    priority_boost: int
    pitch_angle: str


@dataclass
class PracticeGroup:
    group_id: str
    records: list[DoctorRecord]

    @property
    def size(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class WorkflowFields:
    campaign: str
    activation_priority: str
    activation_score: int
    priority_reason: str
    profile_completeness_score: int
    missing_profile_fields: str
    practice_group_id: str
    practice_group_size: int
    other_doctors_at_location: str


CAMPAIGN_PRESETS = {
    "primary-care": CampaignPreset(
        name="primary-care",
        label="Primary Care",
        specialty_terms=(
            "family medicine",
            "internal medicine",
            "general practice",
            "pediatrics",
            "207q00000x",
            "207r00000x",
            "208000000x",
            "208d00000x",
        ),
        priority_boost=18,
        pitch_angle="primary care practice",
    ),
    "dermatology": CampaignPreset(
        name="dermatology",
        label="Dermatology",
        specialty_terms=("dermatology", "207n00000x"),
        priority_boost=22,
        pitch_angle="dermatology practice",
    ),
    "cardiology": CampaignPreset(
        name="cardiology",
        label="Cardiology",
        specialty_terms=("cardiology", "cardiovascular disease", "207rc0000x"),
        priority_boost=22,
        pitch_angle="cardiology practice",
    ),
    "urgent-care": CampaignPreset(
        name="urgent-care",
        label="Urgent Care",
        specialty_terms=("urgent care", "emergency medicine", "family medicine", "207p00000x", "207q00000x"),
        priority_boost=18,
        pitch_angle="urgent care practice",
    ),
}


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
    specialty_filters: list[str] | None = None,
    stats: ImportStats,
    progress: Callable[[str], None] | None = None,
) -> list[DoctorRecord]:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    records_by_npi: "OrderedDict[str, DoctorRecord]" = OrderedDict()

    for zip_index, raw_zip in enumerate(zip_codes, start=1):
        zip_code = normalize_zip_code(raw_zip)
        _progress(progress, f"[{zip_index}/{stats.zip_count}] Starting ZIP {zip_code}")
        seen_page_signatures: set[tuple[str, ...]] = set()
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
                stats.add_skip("malformed_response")
                continue
            page_signature = _page_signature(results)
            if page_signature and page_signature in seen_page_signatures:
                stats.repeated_pages_stopped += 1
                _progress(
                    progress,
                    (
                        f"[{zip_code}] Page {page_index}: CMS returned a repeated page; "
                        "stopping this ZIP"
                    ),
                )
                break
            if page_signature:
                seen_page_signatures.add(page_signature)
            stats.source_records += len(results)
            before_imported = stats.imported_records
            before_skipped = stats.skipped_records
            before_duplicates = stats.duplicate_npis
            for result in results:
                if not isinstance(result, Mapping):
                    stats.add_skip("malformed_record")
                    continue
                record, skip_reason = normalize_result_with_reason(
                    result,
                    matched_zip=zip_code,
                    fetched_at=fetched_at,
                )
                if record is None:
                    stats.add_skip(skip_reason or "malformed_record")
                    continue
                if not _matches_specialty(record, specialty_filters):
                    stats.add_skip("specialty_filter_mismatch")
                    continue
                existing = records_by_npi.get(record.npi)
                if existing is None:
                    records_by_npi[record.npi] = record
                    stats.imported_records += 1
                else:
                    existing.add_matched_zip(zip_code)
                    stats.duplicate_npis += 1
                    stats.add_skip("duplicate_npi")
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
    record, _ = normalize_result_with_reason(result, matched_zip=matched_zip, fetched_at=fetched_at)
    return record


def normalize_result_with_reason(
    result: Mapping[str, object],
    *,
    matched_zip: str,
    fetched_at: str,
) -> tuple[DoctorRecord | None, str | None]:
    if str(result.get("enumeration_type") or "").upper() not in ("", "NPI-1"):
        return None, "non_individual_provider"

    basic = _mapping(result.get("basic"))
    if not _is_active(basic):
        return None, "inactive_or_deactivated"

    taxonomy = _select_physician_taxonomy(result.get("taxonomies"))
    if taxonomy is None:
        return None, "non_physician_taxonomy"

    query_zip = normalize_zip_code(matched_zip)
    address = _select_practice_address(result.get("addresses"), matched_zip=query_zip)
    if address is None:
        return None, "practice_zip_mismatch"

    npi = _clean(result.get("number"))
    if not npi:
        return None, "malformed_record"

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
    return record, None


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
    parser.add_argument(
        "--specialty",
        action="append",
        default=[],
        help="Filter physicians by specialty text, e.g. dermatology. Can be repeated.",
    )
    parser.add_argument(
        "--campaign",
        choices=sorted(CAMPAIGN_PRESETS),
        default=None,
        help="Use a built-in Rox campaign preset for targeting, scoring, and outreach drafts.",
    )
    parser.add_argument(
        "--publish-dry-run",
        action="store_true",
        help="Generate future Certumalink publish payloads. Default output bundles already include this.",
    )
    parser.add_argument(
        "--publish-to-certumalink",
        action="store_true",
        help="POST draft profiles to Certumalink using CERTUMALINK_API_URL and CERTUMALINK_API_TOKEN.",
    )
    parser.add_argument(
        "--status-ledger",
        default=None,
        help="Optional persistent activation status CSV keyed by NPI.",
    )
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
    parser.add_argument(
        "--update",
        action="store_true",
        help="Download and install the latest hosted version of certumalink_run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.update:
            return _update_self()

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
        campaign = _campaign_for_name(args.campaign)
        specialty_filters = _normalize_specialty_filters(args.specialty)
        import_filters = _combined_specialty_filters(specialty_filters, campaign)
        bundle = _resolve_output_bundle(
            zip_codes,
            out_value=args.out,
            output_format=args.format,
            force_bundle=args.publish_dry_run or args.publish_to_certumalink or bool(args.status_ledger),
        )
        progress = None if args.quiet else _print_progress
        _progress(progress, "Preparing CMS NPPES physician import")
        _progress(progress, f"ZIPs queued: {len(zip_codes)}")
        if campaign is not None:
            _progress(progress, f"Campaign: {campaign.label}")
        if import_filters:
            _progress(progress, f"Specialty filter: {', '.join(import_filters)}")
        if max_pages is not None:
            _progress(progress, f"Page limit: {max_pages} page(s) per ZIP")
        _progress(progress, f"Output will be written to: {bundle.output_dir}")
        stats = ImportStats(zip_count=len(zip_codes))
        records = import_zip_codes(
            zip_codes,
            client=NppesClient(),
            fixture_path=Path(args.fixture) if args.fixture else None,
            max_pages_per_zip=max_pages,
            specialty_filters=import_filters,
            stats=stats,
            progress=progress,
        )
        _progress(progress, f"Writing {len(records)} physician records to {bundle.doctors_path}")
        export_records(
            records,
            bundle.doctors_path,
            output_format="csv" if bundle.bundle_mode else args.format,
        )
        bundle_outputs = _write_bundle_outputs(
            bundle,
            records,
            stats=stats,
            zip_codes=zip_codes,
            specialty_filters=import_filters,
            campaign=campaign,
            status_ledger_path=Path(args.status_ledger) if args.status_ledger else None,
            publish_dry_run=args.publish_dry_run or bundle.bundle_mode,
            publish_to_certumalink=args.publish_to_certumalink,
            progress=progress,
        )
        _progress(progress, "Validating export file")
        report = validate_export(bundle.doctors_path)
        _progress(progress, "Import complete")

        if args.json_log:
            payload = stats.to_log_payload()
            payload["event"] = "import_summary"
            payload["exported_records"] = len(records)
            payload["out_path"] = str(bundle.output_dir)
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        _print_report(
            stats=stats,
            exported_records=len(records),
            out_path=bundle.output_dir,
            zip_codes=zip_codes,
            report=report,
            bundle_outputs=bundle_outputs,
            specialty_filters=import_filters,
        )
        publish_summary = bundle_outputs.get("certumalink_publish")
        publish_failed = (
            isinstance(publish_summary, Mapping)
            and bool(publish_summary.get("attempted"))
            and not bool(publish_summary.get("ok"))
        )
        return 0 if report.is_valid and not publish_failed else 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def infer_format(path: Path) -> str:
    return "json" if path.suffix.lower() == ".json" else "csv"


def _resolve_output_bundle(
    zip_codes: list[str],
    *,
    out_value: str | None,
    output_format: str | None,
    force_bundle: bool,
) -> OutputBundle:
    if out_value:
        out_path = Path(out_value)
        is_file_output = out_path.suffix.lower() in (".csv", ".json")
        if is_file_output and not force_bundle:
            return OutputBundle(
                bundle_mode=False,
                output_dir=out_path,
                doctors_path=out_path,
            )
        output_dir = out_path if not is_file_output else out_path.with_suffix("")
    else:
        output_dir = _default_bundle_dir(zip_codes)

    output_dir.mkdir(parents=True, exist_ok=True)
    return OutputBundle(
        bundle_mode=True,
        output_dir=output_dir,
        doctors_path=output_dir / "doctors.csv",
        profile_drafts_path=output_dir / "profile_drafts.csv",
        rox_outreach_path=output_dir / "rox_outreach.csv",
        rox_today_path=output_dir / "rox_today.csv",
        practice_groups_path=output_dir / "practice_groups.csv",
        publish_payload_path=output_dir / "publish_payload.json",
        publish_result_path=output_dir / "publish_result.json",
        activation_status_path=output_dir / "activation_status.csv",
        summary_path=output_dir / "summary.json",
    )


def _default_bundle_dir(zip_codes: list[str]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if len(zip_codes) == 1:
        name = f"certumalink-import-{zip_codes[0]}-{stamp}"
    else:
        name = f"certumalink-import-{len(zip_codes)}-zips-{stamp}"
    return Path.cwd() / name


def _update_self(target_path: Path | None = None) -> int:
    source_url = os.environ.get("CERTUMALINK_IMPORTER_URL", SCRIPT_DOWNLOAD_URL).strip()
    if not source_url:
        raise ValueError("CERTUMALINK_IMPORTER_URL cannot be empty")

    target = (target_path or Path(__file__)).resolve()
    temp_path = target.with_name(f".{target.name}.tmp")
    print("Updating certumalink_run from hosted source...")
    request = Request(
        source_url,
        headers={"User-Agent": "certumalink-doctor-import/0.1"},
    )
    with urlopen(request, timeout=30) as response:
        source = response.read().decode("utf-8")

    _validate_update_source(source)
    try:
        temp_path.write_text(source, encoding="utf-8")
        current_mode = target.stat().st_mode if target.exists() else 0o755
        os.chmod(temp_path, current_mode | 0o111)
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    print("Updated certumalink_run to the latest hosted version.")
    print(f"Installed script: {target}")
    print("Try:")
    print("  certumalink_run --zip 49506")
    return 0


def _validate_update_source(source: str) -> None:
    if "CMS_API_URL" not in source or "def main(" not in source:
        raise ValueError("downloaded update does not look like the Certumalink importer")
    compile(source, SCRIPT_DOWNLOAD_URL, "exec")


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


def _normalize_specialty_filters(values: list[str]) -> list[str]:
    filters: list[str] = []
    for value in values:
        for part in str(value).split(","):
            cleaned = part.strip().lower()
            if cleaned:
                filters.append(cleaned)
    return _dedupe(filters)


def _campaign_for_name(name: str | None) -> CampaignPreset | None:
    if name is None:
        return None
    return CAMPAIGN_PRESETS[name]


def _combined_specialty_filters(
    specialty_filters: list[str],
    campaign: CampaignPreset | None,
) -> list[str]:
    terms = list(specialty_filters)
    if campaign is not None:
        terms.extend(campaign.specialty_terms)
    return _dedupe(terms)


def _matches_specialty(record: DoctorRecord, specialty_filters: list[str] | None) -> bool:
    if not specialty_filters:
        return True
    specialty = record.primary_specialty.lower()
    taxonomy = record.primary_taxonomy_code.lower()
    return any(term in specialty or term == taxonomy for term in specialty_filters)


def _write_bundle_outputs(
    bundle: OutputBundle,
    records: list[DoctorRecord],
    *,
    stats: ImportStats,
    zip_codes: list[str],
    specialty_filters: list[str],
    campaign: CampaignPreset | None,
    status_ledger_path: Path | None,
    publish_dry_run: bool,
    publish_to_certumalink: bool,
    progress: Callable[[str], None] | None,
) -> dict[str, object]:
    if not bundle.bundle_mode:
        return {
            "bundle_mode": False,
            "doctors_path": str(bundle.doctors_path),
            "profile_drafts": 0,
            "rox_outreach": 0,
            "rox_today": 0,
            "practice_groups": 0,
            "publish_payloads": 0,
            "status_counts": {},
            "paths": {"doctors": str(bundle.doctors_path)},
        }

    existing_statuses = _read_status_ledger(status_ledger_path)
    status_rows = _build_activation_status_rows(records, existing_statuses)
    status_by_npi = {row["npi"]: row["activation_status"] for row in status_rows}
    practice_groups = _build_practice_groups(records)
    group_by_npi = _group_by_npi(practice_groups)
    workflow_by_npi = {
        record.npi: _workflow_fields(
            record,
            activation_status=status_by_npi[record.npi],
            campaign=campaign,
            practice_group=group_by_npi[record.npi],
        )
        for record in records
    }
    publish_payload = _publish_payload(
        records,
        status_by_npi,
        campaign=campaign,
        workflow_by_npi=workflow_by_npi,
    ) if publish_dry_run else {
        "dry_run": True,
        "profiles": [],
    }

    publish_result: dict[str, object] | None = None
    if publish_to_certumalink:
        _progress(progress, "Publishing draft profiles to Certumalink")
        publish_result = _publish_to_certumalink(
            _publish_payload(
                records,
                status_by_npi,
                campaign=campaign,
                workflow_by_npi=workflow_by_npi,
                dry_run=False,
            )
        )

    claim_urls_by_npi = _claim_urls_by_npi(publish_result)
    profile_rows = [
        _profile_draft_row(
            record,
            status_by_npi[record.npi],
            workflow_by_npi[record.npi],
            claim_urls_by_npi.get(record.npi, ""),
        )
        for record in records
    ]
    rox_rows = [
        _rox_outreach_row(
            record,
            status_by_npi[record.npi],
            workflow_by_npi[record.npi],
            claim_urls_by_npi.get(record.npi, ""),
            campaign,
        )
        for record in records
    ]
    rox_today_rows = _rox_today_rows(rox_rows)
    practice_group_rows = _practice_group_rows(practice_groups)

    _progress(progress, f"Writing profile drafts to {bundle.profile_drafts_path}")
    _write_csv(profile_rows, PROFILE_DRAFT_FIELDS, bundle.profile_drafts_path)
    _progress(progress, f"Writing Rox outreach CSV to {bundle.rox_outreach_path}")
    _write_csv(rox_rows, ROX_OUTREACH_FIELDS, bundle.rox_outreach_path)
    _progress(progress, f"Writing Rox daily queue to {bundle.rox_today_path}")
    _write_csv(rox_today_rows, ROX_TODAY_FIELDS, bundle.rox_today_path)
    _progress(progress, f"Writing practice groups to {bundle.practice_groups_path}")
    _write_csv(practice_group_rows, PRACTICE_GROUP_FIELDS, bundle.practice_groups_path)
    _progress(progress, f"Writing activation status ledger to {bundle.activation_status_path}")
    _write_csv(status_rows, ACTIVATION_STATUS_FIELDS, bundle.activation_status_path)

    if status_ledger_path is not None:
        _progress(progress, f"Updating persistent status ledger at {status_ledger_path}")
        _write_csv(_merge_status_rows(existing_statuses, status_rows), ACTIVATION_STATUS_FIELDS, status_ledger_path)

    _progress(progress, f"Writing publish dry-run payload to {bundle.publish_payload_path}")
    _write_json(publish_payload, bundle.publish_payload_path)

    if publish_to_certumalink:
        _progress(progress, f"Writing Certumalink publish result to {bundle.publish_result_path}")
        _write_json(publish_result, bundle.publish_result_path)

    status_counts = _status_counts(status_rows)
    priority_counts = _priority_counts(workflow_by_npi.values())
    summary = {
        "bundle_mode": True,
        "zip_codes": zip_codes,
        "campaign": campaign.name if campaign is not None else "",
        "specialty_filters": specialty_filters,
        "stats": stats.to_log_payload(),
        "profile_drafts": len(profile_rows),
        "rox_outreach": len(rox_rows),
        "rox_today": len(rox_today_rows),
        "practice_groups": len(practice_group_rows),
        "publish_payloads": len(publish_payload.get("profiles", [])),
        "certumalink_publish": _publish_summary(publish_result),
        "status_counts": status_counts,
        "priority_counts": priority_counts,
        "average_profile_completeness": _average_profile_completeness(workflow_by_npi.values()),
        "paths": {
            "doctors": str(bundle.doctors_path),
            "profile_drafts": str(bundle.profile_drafts_path),
            "rox_outreach": str(bundle.rox_outreach_path),
            "rox_today": str(bundle.rox_today_path),
            "practice_groups": str(bundle.practice_groups_path),
            "publish_payload": str(bundle.publish_payload_path),
            "publish_result": str(bundle.publish_result_path) if publish_result is not None else "",
            "activation_status": str(bundle.activation_status_path),
            "summary": str(bundle.summary_path),
        },
    }
    _progress(progress, f"Writing summary JSON to {bundle.summary_path}")
    _write_json(summary, bundle.summary_path)
    return summary


def _profile_draft_row(
    record: DoctorRecord,
    activation_status: str,
    workflow: WorkflowFields,
    claim_url: str = "",
) -> dict[str, str]:
    slug = _profile_slug(record)
    return {
        "npi": record.npi,
        "profile_url": _profile_url(record),
        "profile_slug": slug,
        "claim_url": claim_url,
        "display_name": record.display_name,
        "first_name": record.first_name,
        "last_name": record.last_name,
        "credential": record.credential,
        "specialty": record.primary_specialty,
        "taxonomy_code": record.primary_taxonomy_code,
        "city": record.practice_city,
        "state": record.practice_state,
        "practice_zip": record.practice_zip,
        "practice_phone": record.practice_phone,
        "source": record.source,
        "source_fetched_at": record.source_fetched_at,
        "campaign": workflow.campaign,
        "activation_status": activation_status,
        "activation_priority": workflow.activation_priority,
        "activation_score": str(workflow.activation_score),
        "priority_reason": workflow.priority_reason,
        "profile_completeness_score": str(workflow.profile_completeness_score),
        "missing_profile_fields": workflow.missing_profile_fields,
        "practice_group_id": workflow.practice_group_id,
        "practice_group_size": str(workflow.practice_group_size),
        "other_doctors_at_location": workflow.other_doctors_at_location,
    }


def _rox_outreach_row(
    record: DoctorRecord,
    activation_status: str,
    workflow: WorkflowFields,
    claim_url: str,
    campaign: CampaignPreset | None,
) -> dict[str, str]:
    drafts = _rox_editable_drafts(record, campaign, claim_url)
    return {
        "npi": record.npi,
        "doctor_name": record.display_name,
        "campaign": workflow.campaign,
        "specialty": record.primary_specialty,
        "practice_phone": record.practice_phone,
        "city": record.practice_city,
        "state": record.practice_state,
        "profile_url": _profile_url(record),
        "claim_url": claim_url,
        "activation_status": activation_status,
        "activation_priority": workflow.activation_priority,
        "activation_score": str(workflow.activation_score),
        "priority_reason": workflow.priority_reason,
        "profile_completeness_score": str(workflow.profile_completeness_score),
        "missing_profile_fields": workflow.missing_profile_fields,
        "practice_group_id": workflow.practice_group_id,
        "practice_group_size": str(workflow.practice_group_size),
        "other_doctors_at_location": workflow.other_doctors_at_location,
        "suggested_pitch": _suggested_pitch(record),
        "call_opener_draft": drafts["call_opener_draft"],
        "voicemail_draft": drafts["voicemail_draft"],
        "email_subject_draft": drafts["email_subject_draft"],
        "email_body_draft": drafts["email_body_draft"],
        "follow_up_draft": drafts["follow_up_draft"],
    }


def _publish_payload(
    records: list[DoctorRecord],
    status_by_npi: dict[str, str],
    *,
    campaign: CampaignPreset | None = None,
    workflow_by_npi: dict[str, WorkflowFields] | None = None,
    dry_run: bool = True,
) -> dict[str, object]:
    if workflow_by_npi is None:
        practice_groups = _build_practice_groups(records)
        group_by_npi = _group_by_npi(practice_groups)
        workflow_by_npi = {
            record.npi: _workflow_fields(
                record,
                activation_status=status_by_npi[record.npi],
                campaign=campaign,
                practice_group=group_by_npi[record.npi],
            )
            for record in records
        }
    return {
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": SOURCE,
        "campaign": campaign.name if campaign is not None else "",
        "profiles": [
            _profile_draft_row(record, status_by_npi[record.npi], workflow_by_npi[record.npi])
            for record in records
        ],
    }


def _publish_to_certumalink(payload: dict[str, object]) -> dict[str, object]:
    api_url = os.environ.get("CERTUMALINK_API_URL", "").strip().rstrip("/")
    api_token = os.environ.get("CERTUMALINK_API_TOKEN", "").strip()
    if not api_url:
        raise ValueError("CERTUMALINK_API_URL is required for --publish-to-certumalink")
    if not api_token:
        raise ValueError("CERTUMALINK_API_TOKEN is required for --publish-to-certumalink")

    endpoint = f"{api_url}/api/admin/imports/physician-profiles"
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": "certumalink-doctor-import/0.1",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            parsed = json.loads(response_body) if response_body else {}
            if not isinstance(parsed, Mapping):
                parsed = {"response": parsed}
            return {
                "ok": 200 <= int(response.status) < 300,
                "status": int(response.status),
                "endpoint": endpoint,
                "response": dict(parsed),
            }
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_error = json.loads(response_body) if response_body else {}
        except json.JSONDecodeError:
            parsed_error = {"message": response_body}
        return {
            "ok": False,
            "status": int(exc.code),
            "endpoint": endpoint,
            "response": parsed_error,
        }
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Certumalink publish request failed: {exc}") from exc


def _publish_summary(publish_result: dict[str, object] | None) -> dict[str, object]:
    if publish_result is None:
        return {"attempted": False}
    response = publish_result.get("response")
    response_map = response if isinstance(response, Mapping) else {}
    return {
        "attempted": True,
        "ok": bool(publish_result.get("ok")),
        "status": publish_result.get("status"),
        "import_id": response_map.get("import_id") or response_map.get("id") or "",
        "created": response_map.get("created_count", 0),
        "updated": response_map.get("updated_count", 0),
        "unchanged": response_map.get("unchanged_count", 0),
        "skipped": response_map.get("skipped_count", 0),
        "errors": response_map.get("error_count", 0),
        "claim_links": len(_claim_urls_by_npi(publish_result)),
    }


def _claim_urls_by_npi(publish_result: dict[str, object] | None) -> dict[str, str]:
    if publish_result is None:
        return {}
    response = publish_result.get("response")
    if not isinstance(response, Mapping):
        return {}
    results = response.get("results", [])
    if not isinstance(results, list):
        return {}
    claim_urls: dict[str, str] = {}
    for item in results:
        if not isinstance(item, Mapping):
            continue
        npi = _clean(item.get("npi"))
        claim_url = _clean(item.get("claim_url"))
        if npi and claim_url:
            claim_urls[npi] = claim_url
    return claim_urls


def _build_practice_groups(records: list[DoctorRecord]) -> list[PracticeGroup]:
    groups_by_key: "OrderedDict[str, PracticeGroup]" = OrderedDict()
    for record in records:
        key = _practice_group_key(record)
        group = groups_by_key.get(key)
        if group is None:
            group = PracticeGroup(group_id=_practice_group_id(key), records=[])
            groups_by_key[key] = group
        group.records.append(record)
    return list(groups_by_key.values())


def _group_by_npi(practice_groups: list[PracticeGroup]) -> dict[str, PracticeGroup]:
    groups: dict[str, PracticeGroup] = {}
    for group in practice_groups:
        for record in group.records:
            groups[record.npi] = group
    return groups


def _practice_group_key(record: DoctorRecord) -> str:
    phone = _digits_only(record.practice_phone)
    address_parts = [
        record.practice_address_1,
        record.practice_address_2,
        record.practice_city,
        record.practice_state,
        record.practice_zip,
    ]
    address = "|".join(_clean(part).lower() for part in address_parts)
    return f"{phone}|{address}" if phone or address.strip("|") else record.npi


def _practice_group_id(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"practice-{digest}"


def _practice_group_rows(practice_groups: list[PracticeGroup]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for group in sorted(practice_groups, key=lambda item: (-item.size, item.group_id)):
        first = group.records[0]
        rows.append(
            {
                "practice_group_id": group.group_id,
                "practice_group_size": str(group.size),
                "practice_phone": first.practice_phone,
                "practice_address_1": first.practice_address_1,
                "practice_address_2": first.practice_address_2,
                "practice_city": first.practice_city,
                "practice_state": first.practice_state,
                "practice_zip": first.practice_zip,
                "doctors": " | ".join(record.display_name for record in group.records),
                "npi_list": ",".join(record.npi for record in group.records),
            }
        )
    return rows


def _workflow_fields(
    record: DoctorRecord,
    *,
    activation_status: str,
    campaign: CampaignPreset | None,
    practice_group: PracticeGroup,
) -> WorkflowFields:
    completeness_score, missing_fields = _profile_completeness(record)
    score = 0
    reasons: list[str] = []

    if record.practice_phone:
        score += 25
        reasons.append("has practice phone")
    else:
        reasons.append("missing practice phone")
    if record.first_name and record.last_name:
        score += 10
    if record.primary_specialty and record.primary_taxonomy_code:
        score += 15
    if record.practice_address_1 and record.practice_city and record.practice_state and record.practice_zip:
        score += 15
    if campaign is not None and _matches_specialty(record, list(campaign.specialty_terms)):
        score += campaign.priority_boost
        reasons.append(f"matches {campaign.label} campaign")
    if practice_group.size > 1:
        score += 5
        reasons.append(f"shared practice with {practice_group.size} doctors")
    if activation_status in ("not_contacted", "queued_today"):
        score += 5
        reasons.append("not contacted yet")
    if completeness_score >= 90:
        score += 10
    elif completeness_score < 70:
        score -= 10

    if activation_status == "do_not_contact":
        priority = "low"
        reason = "status is do_not_contact"
    elif activation_status == "physician_activated":
        priority = "low"
        reason = "already activated"
    elif activation_status == "needs_review" or completeness_score < 70:
        priority = "low"
        reason = f"needs review: missing {', '.join(missing_fields) or 'profile data'}"
    elif score >= 75:
        priority = "high"
        reason = "; ".join(reasons[:3]) or "high activation fit"
    elif score >= 50:
        priority = "medium"
        reason = "; ".join(reasons[:3]) or "moderate activation fit"
    else:
        priority = "low"
        reason = "; ".join(reasons[:3]) or "low activation fit"

    other_doctors = [
        other.display_name
        for other in practice_group.records
        if other.npi != record.npi
    ]
    return WorkflowFields(
        campaign=campaign.name if campaign is not None else "",
        activation_priority=priority,
        activation_score=max(score, 0),
        priority_reason=reason,
        profile_completeness_score=completeness_score,
        missing_profile_fields=",".join(missing_fields),
        practice_group_id=practice_group.group_id,
        practice_group_size=practice_group.size,
        other_doctors_at_location=" | ".join(other_doctors),
    )


def _profile_completeness(record: DoctorRecord) -> tuple[int, list[str]]:
    fields = [
        ("first_name", record.first_name),
        ("last_name", record.last_name),
        ("specialty", record.primary_specialty),
        ("taxonomy_code", record.primary_taxonomy_code),
        ("practice_address_1", record.practice_address_1),
        ("practice_city", record.practice_city),
        ("practice_state", record.practice_state),
        ("practice_zip", record.practice_zip),
        ("practice_phone", record.practice_phone),
    ]
    missing = [name for name, value in fields if not _clean(value)]
    present = len(fields) - len(missing)
    return round((present / len(fields)) * 100), missing


def _rox_today_rows(rox_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    eligible = [
        row for row in rox_rows
        if row["activation_status"] not in QUEUE_EXCLUDED_STATUSES
        and row["activation_priority"] != "low"
    ]
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    sorted_rows = sorted(
        eligible,
        key=lambda row: (
            priority_rank.get(row["activation_priority"], 9),
            -int(row["activation_score"] or "0"),
            row["doctor_name"],
            row["npi"],
        ),
    )
    return [
        {"queue_rank": str(index), **row}
        for index, row in enumerate(sorted_rows, start=1)
    ]


def _priority_counts(workflows: Iterable[WorkflowFields]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for workflow in workflows:
        counts[workflow.activation_priority] = counts.get(workflow.activation_priority, 0) + 1
    return counts


def _average_profile_completeness(workflows: Iterable[WorkflowFields]) -> int:
    values = [workflow.profile_completeness_score for workflow in workflows]
    if not values:
        return 0
    return round(sum(values) / len(values))


def _digits_only(value: str) -> str:
    return re.sub(r"\D+", "", value)


def _build_activation_status_rows(
    records: list[DoctorRecord],
    existing_statuses: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    seen_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows: list[dict[str, str]] = []
    for record in records:
        existing = existing_statuses.get(record.npi, {})
        activation_status = _normalize_activation_status(existing.get("activation_status", ""))
        if activation_status not in VALID_ACTIVATION_STATUSES:
            raise ValueError(f"invalid activation status for NPI {record.npi}: {activation_status}")
        rows.append(
            {
                "npi": record.npi,
                "activation_status": activation_status,
                "profile_url": _profile_url(record),
                "display_name": record.display_name,
                "specialty": record.primary_specialty,
                "practice_zip": record.practice_zip,
                "last_seen_at": seen_at,
            }
        )
    return rows


def _merge_status_rows(
    existing_statuses: dict[str, dict[str, str]],
    current_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged = dict(existing_statuses)
    for row in current_rows:
        merged[row["npi"]] = row
    return [merged[npi] for npi in sorted(merged)]


def _read_status_ledger(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8") as input_file:
        rows = list(csv.DictReader(input_file))
    statuses: dict[str, dict[str, str]] = {}
    for row in rows:
        npi = str(row.get("npi", "")).strip()
        status = _normalize_activation_status(str(row.get("activation_status", "")).strip())
        if not npi:
            continue
        if status and status not in VALID_ACTIVATION_STATUSES:
            raise ValueError(f"invalid activation status for NPI {npi}: {status}")
        statuses[npi] = {
            "npi": npi,
            "activation_status": status or DEFAULT_ACTIVATION_STATUS,
            "profile_url": str(row.get("profile_url", "")).strip(),
            "display_name": str(row.get("display_name", "")).strip(),
            "specialty": str(row.get("specialty", "")).strip(),
            "practice_zip": str(row.get("practice_zip", "")).strip(),
            "last_seen_at": str(row.get("last_seen_at", "")).strip(),
        }
    return statuses


def _normalize_activation_status(status: str) -> str:
    cleaned = str(status or "").strip()
    if not cleaned:
        return DEFAULT_ACTIVATION_STATUS
    return LEGACY_ACTIVATION_STATUS_MAP.get(cleaned, cleaned)


def _status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row["activation_status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _profile_url(record: DoctorRecord) -> str:
    return f"{CERTUMALINK_BASE_URL}/doctors/{_profile_slug(record)}"


def _profile_slug(record: DoctorRecord) -> str:
    name = " ".join(part for part in (record.first_name, record.last_name) if part) or record.display_name
    base = _slugify(name) or "doctor"
    return f"{base}-{record.npi}"


def _slugify(value: str) -> str:
    lower = value.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")
    return re.sub(r"-+", "-", slug)


def _suggested_pitch(record: DoctorRecord) -> str:
    last_name = record.last_name or record.display_name
    specialty = record.primary_specialty or "medical"
    city = record.practice_city or "your area"
    return (
        f"Dr. {last_name}, Certumalink has prepared a profile for your "
        f"{specialty} practice in {city}. We can help you activate and review it."
    )


def _rox_editable_drafts(
    record: DoctorRecord,
    campaign: CampaignPreset | None,
    claim_url: str,
) -> dict[str, str]:
    last_name = record.last_name or record.display_name
    specialty = campaign.pitch_angle if campaign is not None else (record.primary_specialty or "medical practice")
    city = record.practice_city or "your area"
    activation_target = claim_url or _profile_url(record)
    return {
        "call_opener_draft": (
            f"Hi Dr. {last_name}, this is Rox calling with Certumalink. "
            f"We prepared a draft profile for your {specialty} in {city} and wanted to help you review it."
        ),
        "voicemail_draft": (
            f"Hi Dr. {last_name}, this is Rox with Certumalink. "
            "We prepared a draft physician profile for you and can help activate it when you are ready."
        ),
        "email_subject_draft": "Your Certumalink physician profile is ready for review",
        "email_body_draft": (
            f"Hi Dr. {last_name},\n\n"
            f"Certumalink prepared a draft profile for your {specialty} in {city}. "
            "You can review the profile details and decide whether to activate it for patients.\n\n"
            f"Review link: {activation_target}\n\n"
            "Rox can help update the profile if anything needs to change."
        ),
        "follow_up_draft": (
            f"Hi Dr. {last_name}, following up on the Certumalink profile we prepared. "
            f"When convenient, you can review it here: {activation_target}"
        ),
    }


def _write_csv(rows: list[dict[str, str]], fieldnames: list[str], path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(payload: object, path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2, sort_keys=True)
        output.write("\n")


def _print_report(
    *,
    stats: ImportStats,
    exported_records: int,
    out_path: Path,
    zip_codes: list[str],
    report: ValidationReport,
    bundle_outputs: dict[str, object],
    specialty_filters: list[str],
) -> None:
    shown_zips = ", ".join(zip_codes[:8])
    if len(zip_codes) > 8:
        shown_zips += f", ... ({len(zip_codes)} total)"

    print()
    print("Certumalink Doctor Import Report")
    print("--------------------------------")
    print(f"ZIPs: {shown_zips}")
    campaign = bundle_outputs.get("campaign")
    if campaign:
        print(f"Campaign: {campaign}")
    if specialty_filters:
        print(f"Specialty filter: {', '.join(specialty_filters)}")
    print(f"Output: {out_path}")
    print(f"CMS records scanned: {stats.source_records}")
    print(f"Physicians exported: {exported_records}")
    print(f"Skipped records: {stats.skipped_records}")
    if stats.skip_reasons:
        print("Skip reasons:")
        for reason, count in sorted(stats.skip_reasons.items()):
            print(f"  - {reason}: {count}")
    print(f"Duplicate NPIs merged: {stats.duplicate_npis}")
    print(f"CMS response pages: {stats.response_pages}")
    if stats.repeated_pages_stopped:
        print(f"Repeated CMS pages stopped: {stats.repeated_pages_stopped}")
    if bundle_outputs.get("bundle_mode"):
        print(f"Profile drafts created: {bundle_outputs.get('profile_drafts', 0)}")
        print(f"Rox outreach rows created: {bundle_outputs.get('rox_outreach', 0)}")
        print(f"Rox daily queue rows: {bundle_outputs.get('rox_today', 0)}")
        print(f"Practice groups: {bundle_outputs.get('practice_groups', 0)}")
        print(f"Average profile completeness: {bundle_outputs.get('average_profile_completeness', 0)}")
        priority_counts = bundle_outputs.get("priority_counts") or {}
        if isinstance(priority_counts, Mapping) and priority_counts:
            print("Activation priorities:")
            for priority, count in sorted(priority_counts.items()):
                print(f"  - {priority}: {count}")
        print(f"Publish dry-run payloads: {bundle_outputs.get('publish_payloads', 0)}")
        publish_summary = bundle_outputs.get("certumalink_publish")
        if isinstance(publish_summary, Mapping) and publish_summary.get("attempted"):
            print(
                "Certumalink publish: "
                f"{'passed' if publish_summary.get('ok') else 'failed'} "
                f"(status {publish_summary.get('status')})"
            )
            if publish_summary.get("import_id"):
                print(f"Certumalink import ID: {publish_summary.get('import_id')}")
            print(
                "Certumalink results: "
                f"created {publish_summary.get('created', 0)}, "
                f"updated {publish_summary.get('updated', 0)}, "
                f"unchanged {publish_summary.get('unchanged', 0)}, "
                f"skipped {publish_summary.get('skipped', 0)}, "
                f"errors {publish_summary.get('errors', 0)}"
            )
            print(f"Certumalink claim links returned: {publish_summary.get('claim_links', 0)}")
        status_counts = bundle_outputs.get("status_counts") or {}
        if isinstance(status_counts, Mapping) and status_counts:
            print("Activation statuses:")
            for status, count in sorted(status_counts.items()):
                print(f"  - {status}: {count}")
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


def _page_signature(results: list[object]) -> tuple[str, ...]:
    numbers: list[str] = []
    for result in results:
        if isinstance(result, Mapping):
            numbers.append(_clean(result.get("number")))
    return tuple(numbers)


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
