from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable" / "certumalink-doctor-import.py"
FIXTURE = ROOT / "tests" / "fixtures" / "nppes_mixed_page.json"


def load_portable_module():
    spec = importlib.util.spec_from_file_location("certumalink_doctor_import", PORTABLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PortableV2Tests(unittest.TestCase):
    def test_specialty_filter_counts_mismatches(self) -> None:
        module = load_portable_module()
        stats = module.ImportStats(zip_count=1)

        records = module.import_zip_codes(
            ["78701"],
            client=module.NppesClient(),
            fixture_path=FIXTURE,
            specialty_filters=["pediatrics"],
            stats=stats,
        )

        self.assertEqual([record.primary_specialty for record in records], ["Pediatrics"])
        self.assertEqual(stats.skip_reasons["specialty_filter_mismatch"], 1)
        self.assertEqual(stats.skip_reasons["non_physician_taxonomy"], 1)
        self.assertEqual(stats.skip_reasons["non_individual_provider"], 1)
        self.assertEqual(stats.skip_reasons["inactive_or_deactivated"], 1)

    def test_bundle_outputs_profile_rox_publish_and_status(self) -> None:
        module = load_portable_module()
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory) / "bundle"
            ledger = Path(directory) / "activation_status.csv"
            ledger.write_text(
                "npi,activation_status,profile_url,display_name,specialty,practice_zip,last_seen_at\n"
                "1234567890,rox_contacted,,,,,\n",
                encoding="utf-8",
            )

            exit_code = module.main(
                [
                    "--zip",
                    "78701",
                    "--fixture",
                    str(FIXTURE),
                    "--out",
                    str(out_dir),
                    "--status-ledger",
                    str(ledger),
                ]
            )

            self.assertEqual(exit_code, 0)
            expected_files = {
                "doctors.csv",
                "profile_drafts.csv",
                "rox_outreach.csv",
                "publish_payload.json",
                "activation_status.csv",
                "summary.json",
            }
            self.assertEqual({path.name for path in out_dir.iterdir()}, expected_files)

            with (out_dir / "profile_drafts.csv").open("r", newline="", encoding="utf-8") as input_file:
                profiles = list(csv.DictReader(input_file))
            self.assertEqual(profiles[0]["profile_slug"], "ada-lovelace-1234567890")
            self.assertEqual(
                profiles[0]["profile_url"],
                "https://www.certumalink.com/doctors/ada-lovelace-1234567890",
            )
            self.assertEqual(profiles[0]["activation_status"], "rox_contacted")

            with (out_dir / "rox_outreach.csv").open("r", newline="", encoding="utf-8") as input_file:
                rox_rows = list(csv.DictReader(input_file))
            self.assertIn("Certumalink has prepared a profile", rox_rows[0]["suggested_pitch"])

            payload = json.loads((out_dir / "publish_payload.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["dry_run"])
            self.assertEqual(len(payload["profiles"]), 2)

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["profile_drafts"], 2)
            self.assertEqual(summary["rox_outreach"], 2)
            self.assertEqual(summary["publish_payloads"], 2)
            self.assertEqual(summary["status_counts"]["rox_contacted"], 1)
            self.assertEqual(summary["status_counts"]["draft_profile_created"], 1)


if __name__ == "__main__":
    unittest.main()
