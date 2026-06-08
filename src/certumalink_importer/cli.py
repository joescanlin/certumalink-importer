from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .exporters import export_records
from .importer import ImportStats, import_zip_codes
from .nppes import NppesClient
from .validation import validate_export
from .zipcodes import read_zip_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="certumalink-nppes",
        description="Import public CMS NPPES physician records by ZIP code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_zip = subparsers.add_parser("import-zip", help="Import one ZIP code.")
    import_zip.add_argument("--zip", required=True, dest="zip_code", help="5-digit ZIP code.")
    import_zip.add_argument("--out", required=True, help="Output CSV or JSON path.")
    import_zip.add_argument(
        "--format",
        choices=("csv", "json"),
        default=None,
        help="Output format. Defaults to the file extension.",
    )
    import_zip.add_argument(
        "--fixture",
        default=None,
        help="Read a CMS-like JSON response from disk instead of calling the API.",
    )
    import_zip.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum CMS response pages to import per ZIP. Useful for live smoke tests.",
    )

    import_zips = subparsers.add_parser("import-zips", help="Import a CSV/TXT list of ZIP codes.")
    import_zips.add_argument("--zip-file", required=True, help="CSV/TXT file with ZIP codes.")
    import_zips.add_argument("--out", required=True, help="Output CSV or JSON path.")
    import_zips.add_argument(
        "--format",
        choices=("csv", "json"),
        default=None,
        help="Output format. Defaults to the file extension.",
    )
    import_zips.add_argument(
        "--fixture",
        default=None,
        help="Read a CMS-like JSON response from disk for every ZIP instead of calling the API.",
    )
    import_zips.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum CMS response pages to import per ZIP. Useful for live smoke tests.",
    )

    validate = subparsers.add_parser("validate-export", help="Validate an exported CSV or JSON file.")
    validate.add_argument("path", help="Export file path.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "import-zip":
            stats = ImportStats(zip_count=1)
            records = import_zip_codes(
                [args.zip_code],
                client=NppesClient(),
                fixture_path=Path(args.fixture) if args.fixture else None,
                stats=stats,
                max_pages_per_zip=_max_pages(args.max_pages),
            )
            export_records(records, Path(args.out), output_format=args.format)
            _print_import_log(stats, exported_records=len(records), out_path=args.out)
            print(f"Exported {len(records)} physician records to {args.out}")
            return 0

        if args.command == "import-zips":
            zip_codes = read_zip_file(Path(args.zip_file))
            stats = ImportStats(zip_count=len(zip_codes))
            records = import_zip_codes(
                zip_codes,
                client=NppesClient(),
                fixture_path=Path(args.fixture) if args.fixture else None,
                stats=stats,
                max_pages_per_zip=_max_pages(args.max_pages),
            )
            export_records(records, Path(args.out), output_format=args.format)
            _print_import_log(stats, exported_records=len(records), out_path=args.out)
            print(
                f"Exported {len(records)} physician records from "
                f"{len(zip_codes)} ZIP codes to {args.out}"
            )
            return 0

        if args.command == "validate-export":
            report = validate_export(Path(args.path))
            print(report.summary())
            return 0 if report.is_valid else 1

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


def _print_import_log(stats: ImportStats, *, exported_records: int, out_path: str) -> None:
    payload = stats.to_log_payload()
    payload["event"] = "import_summary"
    payload["exported_records"] = exported_records
    payload["out_path"] = out_path
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _max_pages(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise ValueError("--max-pages must be 1 or greater")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
