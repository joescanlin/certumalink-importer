from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from certumalink_importer.cli import main
from certumalink_importer.exporters import export_records
from certumalink_importer.importer import ImportStats, import_zip_codes
from certumalink_importer.validation import validate_export
from certumalink_importer.zipcodes import read_zip_file


FIXTURE = Path(__file__).parent / "fixtures" / "nppes_mixed_page.json"


class TwoPageClient:
    def __init__(self) -> None:
        self.pages_yielded = 0

    def iter_zip_search(self, zip_code: str):
        for _ in range(2):
            self.pages_yielded += 1
            with FIXTURE.open("r", encoding="utf-8") as fixture:
                import json

                yield json.load(fixture)


class MismatchedPracticeZipClient:
    def iter_zip_search(self, zip_code: str):
        yield {
            "results": [
                {
                    "number": 9999999999,
                    "enumeration_type": "NPI-1",
                    "basic": {
                        "first_name": "Query",
                        "last_name": "Mismatch",
                        "credential": "MD",
                        "status": "A",
                    },
                    "taxonomies": [
                        {
                            "code": "207R00000X",
                            "desc": "Internal Medicine",
                            "primary": True,
                        }
                    ],
                    "addresses": [
                        {
                            "address_purpose": "MAILING",
                            "country_code": "US",
                            "address_1": "PO BOX 1",
                            "city": "Austin",
                            "state": "TX",
                            "postal_code": "78701",
                        },
                        {
                            "address_purpose": "LOCATION",
                            "country_code": "US",
                            "address_1": "100 MEDICAL PKWY",
                            "city": "Lakeway",
                            "state": "TX",
                            "postal_code": "78738",
                        },
                    ],
                }
            ]
        }


class DuplicateZipClient:
    def iter_zip_search(self, zip_code: str):
        yield {
            "results": [
                {
                    "number": 1234567890,
                    "enumeration_type": "NPI-1",
                    "basic": {
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "credential": "MD",
                        "status": "A",
                    },
                    "taxonomies": [
                        {
                            "code": "207R00000X",
                            "desc": "Internal Medicine",
                            "primary": True,
                        }
                    ],
                    "addresses": [
                        {
                            "address_purpose": "LOCATION",
                            "country_code": "US",
                            "address_1": "100 MAIN ST",
                            "city": "Austin",
                            "state": "TX",
                            "postal_code": zip_code,
                        }
                    ],
                }
            ]
        }


class RepeatingPageClient:
    def __init__(self) -> None:
        self.pages_yielded = 0

    def iter_zip_search(self, zip_code: str):
        page = {
            "results": [
                {
                    "number": 1234567890,
                    "enumeration_type": "NPI-1",
                    "basic": {
                        "first_name": "Ada",
                        "last_name": "Lovelace",
                        "credential": "MD",
                        "status": "A",
                    },
                    "taxonomies": [
                        {
                            "code": "207R00000X",
                            "desc": "Internal Medicine",
                            "primary": True,
                        }
                    ],
                    "addresses": [
                        {
                            "address_purpose": "LOCATION",
                            "country_code": "US",
                            "address_1": "100 MAIN ST",
                            "city": "Austin",
                            "state": "TX",
                            "postal_code": zip_code,
                        }
                    ],
                }
            ]
        }
        while True:
            self.pages_yielded += 1
            yield page


class ImporterTests(unittest.TestCase):
    def test_reads_zip_file_with_header_and_zip4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zips.csv"
            path.write_text("zip,name\n78701,Austin\n60601-1234,Chicago\n78701,Austin\n", encoding="utf-8")

            self.assertEqual(read_zip_file(path), ["78701", "60601"])

    def test_import_filters_to_active_individual_physicians(self) -> None:
        records = import_zip_codes(["78701"], client=None, fixture_path=FIXTURE)  # type: ignore[arg-type]

        self.assertEqual([record.npi for record in records], ["1234567890", "5678901234"])
        self.assertEqual(records[0].display_name, "Ada M Lovelace, MD")
        self.assertEqual(records[0].primary_taxonomy_code, "207R00000X")
        self.assertEqual(records[0].primary_specialty, "Internal Medicine")
        self.assertEqual(records[0].practice_address_1, "100 MAIN ST")
        self.assertEqual(records[0].practice_zip, "78701")
        self.assertEqual(records[1].practice_phone, "")

    def test_import_dedupes_npi_and_tracks_matched_zips(self) -> None:
        records = import_zip_codes(["78701", "60601"], client=DuplicateZipClient())

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].matched_zips, ["78701", "60601"])

    def test_import_skips_records_when_practice_zip_does_not_match_query(self) -> None:
        client = MismatchedPracticeZipClient()

        records = import_zip_codes(["78701"], client=client)

        self.assertEqual(records, [])

    def test_import_can_limit_pages_per_zip(self) -> None:
        client = TwoPageClient()

        records = import_zip_codes(["78701"], client=client, max_pages_per_zip=1)

        self.assertEqual(len(records), 2)
        self.assertEqual(client.pages_yielded, 1)

    def test_import_stops_when_cms_repeats_a_page(self) -> None:
        client = RepeatingPageClient()
        stats = ImportStats(zip_count=1)

        records = import_zip_codes(["78701"], client=client, stats=stats)

        self.assertEqual(len(records), 1)
        self.assertEqual(client.pages_yielded, 2)
        self.assertEqual(stats.source_records, 1)
        self.assertEqual(stats.repeated_pages_stopped, 1)

    def test_export_and_validate_csv(self) -> None:
        records = import_zip_codes(["78701"], client=None, fixture_path=FIXTURE)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "doctors.csv"
            export_records(records, out)

            report = validate_export(out)
            self.assertTrue(report.is_valid, report.summary())

            with out.open("r", newline="", encoding="utf-8") as input_file:
                rows = list(csv.DictReader(input_file))
            self.assertEqual(rows[0]["source"], "cms_nppes_registry_api")
            self.assertEqual(rows[0]["matched_zips"], "78701")

    def test_cli_fixture_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "doctors.json"
            exit_code = main(
                [
                    "import-zip",
                    "--zip",
                    "78701",
                    "--out",
                    str(out),
                    "--format",
                    "json",
                    "--fixture",
                    str(FIXTURE),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(out.exists())
            self.assertTrue(validate_export(out).is_valid)


if __name__ == "__main__":
    unittest.main()
