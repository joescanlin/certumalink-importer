"""Publish-client tests (Phase 0 task B13). Pure: no DB, no network (opener/fetch injected)."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma import publish  # noqa: E402


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _prospect():
    return SimpleNamespace(
        npi="1497507156", profile_url="https://www.certumalink.com/doctors/m-1497507156",
        profile_slug="m-1497507156", display_name="MOHAMAD", first_name="MOHAMAD",
        last_name="ABOUELNAAJ", credential="", primary_specialty="Internal Medicine",
        primary_taxonomy_code="207R00000X", practice_city="AUSTIN", practice_state="TX",
        practice_zip="78701", practice_phone="512-324-7000", source="cms_nppes_registry_api",
        source_fetched_at=None,
    )


def _score():
    return SimpleNamespace(
        campaign="primary-care", activation_priority="high", activation_score=88,
        priority_reason="has practice phone", profile_completeness_score=100,
        missing_profile_fields=[], practice_group_id="practice-abc", practice_group_size=3,
    )


class PayloadTests(unittest.TestCase):
    def test_profile_row_matches_contract_fields(self):
        lead = SimpleNamespace(claim_url="", activation_status="not_contacted")
        row = publish.profile_payload_row(_prospect(), _score(), lead,
                                          other_doctors=["JANE SMITH, MD", "ROBERT LEE, DO"])
        self.assertEqual(set(row), set(publish.PROFILE_FIELDS))
        self.assertEqual(row["activation_score"], "88")          # serialized to string
        self.assertEqual(row["practice_group_size"], "3")
        self.assertEqual(row["missing_profile_fields"], "")
        self.assertEqual(row["other_doctors_at_location"], "JANE SMITH, MD | ROBERT LEE, DO")
        self.assertEqual(row["activation_status"], "not_contacted")

    def test_profile_row_degrades_without_score_or_lead(self):
        row = publish.profile_payload_row(_prospect(), None, None)
        self.assertEqual(row["activation_status"], "not_contacted")
        self.assertEqual(row["activation_score"], "0")
        self.assertEqual(row["campaign"], "")

    def test_build_payload_envelope(self):
        env = publish.build_payload([{"npi": "1"}], campaign="primary-care",
                                    generated_at="2026-06-23T00:00:00+00:00", dry_run=True)
        self.assertEqual(env["dry_run"], True)
        self.assertEqual(env["campaign"], "primary-care")
        self.assertEqual(env["source"], "cms_nppes_registry_api")
        self.assertEqual(env["generated_at"], "2026-06-23T00:00:00+00:00")
        self.assertEqual(env["profiles"], [{"npi": "1"}])


class ClientTests(unittest.TestCase):
    def test_publish_success(self):
        resp_body = json.dumps({"import_id": "imp_1", "created_count": 1,
                                "results": [{"npi": "1497507156", "claim_url": "https://c/x"}]})

        def opener(req, timeout=None):
            self.assertEqual(req.get_header("Authorization"), "Bearer t0ken")
            self.assertTrue(req.full_url.endswith("/api/admin/imports/physician-profiles"))
            return _FakeResp(200, resp_body)

        result = publish.publish({"dry_run": False}, base_url="https://x", token="t0ken", opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(publish.claim_urls_by_npi(result), {"1497507156": "https://c/x"})
        summary = publish.publish_summary(result)
        self.assertTrue(summary["attempted"] and summary["ok"])
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["claim_links"], 1)

    def test_publish_http_error_returns_result(self):
        def opener(req, timeout=None):
            raise HTTPError(req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"error_count":1}'))

        result = publish.publish({}, base_url="https://x", token="t", opener=opener)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 422)
        self.assertEqual(result["response"]["error_count"], 1)

    def test_publish_transport_error_raises(self):
        def opener(req, timeout=None):
            raise URLError("connection refused")

        with self.assertRaises(publish.PublishError):
            publish.publish({}, base_url="https://x", token="t", opener=opener)

    def test_publish_requires_creds(self):
        with self.assertRaises(ValueError):
            publish.publish({}, base_url="", token="t")
        with self.assertRaises(ValueError):
            publish.publish({}, base_url="https://x", token="")


class ClaimStatusTests(unittest.TestCase):
    def test_default_fetch_raises_until_wired(self):
        with self.assertRaises(publish.ClaimStatusUnavailable):
            publish.poll_claim_urls([("1", "https://c/x")])

    def test_poll_with_injected_fetch(self):
        out = publish.poll_claim_urls(
            [("1", "https://c/1"), ("2", ""), ("3", "https://c/3")],
            fetch=lambda url: "activated" if url.endswith("1") else "pending",
        )
        self.assertEqual(out, {"1": "activated", "3": "pending"})  # npi 2 skipped (no claim_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
