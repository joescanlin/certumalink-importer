from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_publish_to_certumalink_requires_env(self) -> None:
        module = load_portable_module()
        with patch.dict(module.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "CERTUMALINK_API_URL"):
                module._publish_to_certumalink({"dry_run": False, "profiles": []})

        with patch.dict(module.os.environ, {"CERTUMALINK_API_URL": "https://api.certumalink.test"}, clear=True):
            with self.assertRaisesRegex(ValueError, "CERTUMALINK_API_TOKEN"):
                module._publish_to_certumalink({"dry_run": False, "profiles": []})

    def test_publish_to_certumalink_writes_result(self) -> None:
        module = load_portable_module()
        requests = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "import_id": "imp_123",
                        "created_count": 2,
                        "updated_count": 0,
                        "skipped_count": 0,
                        "error_count": 0,
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory) / "bundle"
            with patch.dict(
                module.os.environ,
                {
                    "CERTUMALINK_API_URL": "https://api.certumalink.test",
                    "CERTUMALINK_API_TOKEN": "secret-token",
                },
                clear=True,
            ), patch.object(module, "urlopen", fake_urlopen):
                exit_code = module.main(
                    [
                        "--zip",
                        "78701",
                        "--fixture",
                        str(FIXTURE),
                        "--out",
                        str(out_dir),
                        "--publish-to-certumalink",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                requests[0].full_url,
                "https://api.certumalink.test/api/admin/imports/physician-profiles",
            )
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret-token")
            sent_payload = json.loads(requests[0].data.decode("utf-8"))
            self.assertFalse(sent_payload["dry_run"])
            self.assertEqual(len(sent_payload["profiles"]), 2)

            result = json.loads((out_dir / "publish_result.json").read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["response"]["import_id"], "imp_123")

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["certumalink_publish"]["import_id"], "imp_123")
            self.assertEqual(summary["certumalink_publish"]["created"], 2)

    def test_publish_to_certumalink_http_error_writes_result_and_fails(self) -> None:
        module = load_portable_module()

        class ErrorBody:
            def read(self):
                return b'{"error_count": 1, "message": "invalid payload"}'

            def close(self):
                pass

        def fake_urlopen(request, timeout):
            raise module.HTTPError(
                request.full_url,
                422,
                "Unprocessable Entity",
                {},
                ErrorBody(),
            )

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory) / "bundle"
            with patch.dict(
                module.os.environ,
                {
                    "CERTUMALINK_API_URL": "https://api.certumalink.test",
                    "CERTUMALINK_API_TOKEN": "secret-token",
                },
                clear=True,
            ), patch.object(module, "urlopen", fake_urlopen):
                exit_code = module.main(
                    [
                        "--zip",
                        "78701",
                        "--fixture",
                        str(FIXTURE),
                        "--out",
                        str(out_dir),
                        "--publish-to-certumalink",
                    ]
                )

            self.assertEqual(exit_code, 1)
            result = json.loads((out_dir / "publish_result.json").read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], 422)
            self.assertEqual(result["response"]["error_count"], 1)

    def test_update_flag_does_not_require_zip(self) -> None:
        module = load_portable_module()

        with patch.object(module, "_update_self", return_value=0) as update_self:
            exit_code = module.main(["--update"])

        self.assertEqual(exit_code, 0)
        update_self.assert_called_once_with()

    def test_update_self_replaces_target_script(self) -> None:
        module = load_portable_module()
        requests = []
        updated_source = "\n".join(
            [
                "#!/usr/bin/env python3",
                'CMS_API_URL = "https://npiregistry.cms.hhs.gov/api/"',
                "",
                "def main():",
                "    return 0",
                "",
            ]
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return updated_source.encode("utf-8")

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "certumalink-doctor-import.py"
            target.write_text("old source", encoding="utf-8")
            os.chmod(target, 0o600)

            with patch.dict(
                module.os.environ,
                {"CERTUMALINK_IMPORTER_URL": "https://example.test/certumalink.py"},
                clear=True,
            ), patch.object(module, "urlopen", fake_urlopen):
                exit_code = module._update_self(target)

            self.assertEqual(exit_code, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), updated_source)
            self.assertTrue(target.stat().st_mode & 0o100)
            self.assertEqual(requests[0].full_url, "https://example.test/certumalink.py")


if __name__ == "__main__":
    unittest.main()
