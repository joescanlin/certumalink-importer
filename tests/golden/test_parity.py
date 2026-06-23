"""Golden-master parity: certuma_core must reproduce the monolith byte-for-byte.

Drives certuma_core and portable/certumalink-doctor-import.py with identical inputs
(real export output/live-78701.csv) and asserts identical outputs for scoring, grouping,
completeness, queue ranking, urls, specialty, status normalization, and util helpers.

Run: PYTHONPATH=. python3 -m unittest tests.golden.test_parity
 or: python3 tests/golden/test_parity.py
"""
from __future__ import annotations

import csv
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PORTABLE = ROOT / "portable" / "certumalink-doctor-import.py"
# Tracked golden copy (output/ is gitignored, so the regression lock lives under tests/).
LIVE_CSV = ROOT / "tests" / "golden" / "data" / "live-78701.csv"
FIXTURE = ROOT / "tests" / "fixtures" / "nppes_mixed_page.json"

from certuma_core import campaigns, grouping, queue, scoring, specialty, status, urls  # noqa: E402
from certuma_core.models import DoctorRecord  # noqa: E402

STATUSES = [
    "not_contacted",
    "queued_today",
    "email_sent",
    "interested",
    "do_not_contact",
    "physician_activated",
    "needs_review",
    "called_no_answer",
    "voicemail_left",
]
CAMPAIGN_NAMES = [None, "primary-care", "dermatology", "cardiology", "urgent-care"]

RECORD_FIELDS = [
    "npi", "first_name", "middle_name", "last_name", "credential", "display_name",
    "primary_taxonomy_code", "primary_specialty", "practice_address_1", "practice_address_2",
    "practice_city", "practice_state", "practice_zip", "practice_phone", "source_fetched_at",
]


def load_monolith():
    spec = importlib.util.spec_from_file_location("certumalink_doctor_import", PORTABLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row_kwargs(row: dict) -> dict:
    kw = {name: (row.get(name) or "") for name in RECORD_FIELDS}
    kw["matched_zips"] = [z for z in (row.get("matched_zips") or "").split(",") if z]
    return kw


def load_records(module):
    """Return parallel lists of (monolith records, core records) from the live CSV."""
    mono, core = [], []
    with LIVE_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            kw = _row_kwargs(row)
            mono.append(module.DoctorRecord(**kw))
            core.append(DoctorRecord(**kw))
    return mono, core


def _core_from_mono(mr) -> DoctorRecord:
    return DoctorRecord(
        **{name: getattr(mr, name) for name in RECORD_FIELDS},
        matched_zips=list(mr.matched_zips),
    )


def load_fixture_records(module):
    """Import the engineered NPPES fixture through the monolith, mirror into core records."""
    mono = module.import_zip_codes(
        ["78701"],
        client=module.NppesClient(),
        fixture_path=FIXTURE,
        stats=module.ImportStats(zip_count=1),
    )
    core = [_core_from_mono(mr) for mr in mono]
    return mono, core


class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_monolith()
        cls.mono_records, cls.core_records = load_records(cls.m)
        assert cls.core_records, "live CSV produced no records"
        cls.fix_mono, cls.fix_core = load_fixture_records(cls.m)
        assert cls.fix_core, "fixture produced no records"

    # ---- util ----
    def test_util_helpers(self):
        m = self.m
        samples = [None, "  Hi ", "x", 0, "512-324-7000", " a,b "]
        for s in samples:
            self.assertEqual(__import__("certuma_core.util", fromlist=["clean"]).clean(s), m._clean(s))
        from certuma_core.util import clean, digits_only, dedupe
        for s in ["512-324-7000", "", "abc123", "+1 (512) 324-7000"]:
            self.assertEqual(digits_only(s), m._digits_only(s))
        vals = ["a", "b", "a", "c", "b", "a"]
        self.assertEqual(dedupe(vals), m._dedupe(vals))

    # ---- status normalization ----
    def test_normalize_status(self):
        m = self.m
        for s in ["", "  ", "draft_profile_created", "rox_contacted", "activated",
                  "email_sent", "weird_status", None, "  not_contacted  "]:
            self.assertEqual(status.normalize_status(s), m._normalize_activation_status(s or ""))

    # ---- urls ----
    def test_urls(self):
        m = self.m
        for mr, cr in zip(self.mono_records, self.core_records):
            self.assertEqual(urls.slugify(mr.display_name), m._slugify(mr.display_name))
            self.assertEqual(urls.profile_slug(cr), m._profile_slug(mr))
            self.assertEqual(urls.profile_url(cr), m._profile_url(mr))

    def test_claim_urls_by_npi(self):
        m = self.m
        payload = {"response": {"results": [
            {"npi": "1234567890", "claim_url": "https://x/claim/1"},
            {"npi": "", "claim_url": "https://x/claim/2"},
            {"npi": "1999999999", "claim_url": ""},
            {"npi": "1888888888", "claim_url": "https://x/claim/3"},
            "not-a-mapping",
        ]}}
        self.assertEqual(urls.claim_urls_by_npi(payload), m._claim_urls_by_npi(payload))
        self.assertEqual(urls.claim_urls_by_npi(None), m._claim_urls_by_npi(None))

    # ---- specialty ----
    def test_specialty(self):
        m = self.m
        derm_terms = list(campaigns.CAMPAIGN_PRESETS["dermatology"].specialty_terms)
        filters = ["dermatology", "internal medicine"]
        self.assertEqual(
            specialty.combined_specialty_filters(filters, campaigns.CAMPAIGN_PRESETS["dermatology"]),
            m._combined_specialty_filters(filters, m.CAMPAIGN_PRESETS["dermatology"]),
        )
        for mr, cr in zip(self.mono_records, self.core_records):
            for terms in (derm_terms, [], None, ["207"]):
                self.assertEqual(
                    specialty.matches_specialty(cr, terms),
                    m._matches_specialty(mr, terms),
                    msg=f"matches_specialty diverged for {cr.npi} terms={terms}",
                )

    # ---- grouping ----
    def test_grouping(self):
        m = self.m
        mgroups = m._build_practice_groups(self.mono_records)
        cgroups = grouping.build_practice_groups(self.core_records)
        self.assertEqual(
            [(g.group_id, [r.npi for r in g.records]) for g in mgroups],
            [(g.group_id, [r.npi for r in g.records]) for g in cgroups],
        )
        # practice_group_rows: compare the columns core still emits (drops doctors/npi_list).
        mono_rows = m._practice_group_rows(mgroups)
        core_rows = grouping.practice_group_rows(cgroups)
        self.assertEqual(len(mono_rows), len(core_rows))
        shared = ["practice_group_id", "practice_phone", "practice_address_1",
                  "practice_address_2", "practice_city", "practice_state", "practice_zip"]
        for mrow, crow in zip(mono_rows, core_rows):
            for key in shared:
                self.assertEqual(crow[key], mrow[key], msg=f"group row {key} diverged")
            self.assertEqual(crow["practice_group_size"], int(mrow["practice_group_size"]))

    # ---- completeness ----
    def test_profile_completeness(self):
        m = self.m
        for mr, cr in zip(self.mono_records, self.core_records):
            self.assertEqual(scoring.profile_completeness(cr), m._profile_completeness(mr))

    def _assert_workflow_parity(self, mono_records, core_records):
        m = self.m
        mgroups = m._build_practice_groups(mono_records)
        m_by_npi = m._group_by_npi(mgroups)
        cgroups = grouping.build_practice_groups(core_records)
        c_by_npi = grouping.group_by_npi(cgroups)

        checks = 0
        for i, (mr, cr) in enumerate(zip(mono_records, core_records)):
            st = STATUSES[i % len(STATUSES)]
            for cname in CAMPAIGN_NAMES:
                mcamp = m._campaign_for_name(cname)
                ccamp = campaigns.campaign_or_none(cname)
                mw = m._workflow_fields(mr, activation_status=st, campaign=mcamp,
                                        practice_group=m_by_npi[mr.npi])
                cw = scoring.compute_workflow_fields(cr, activation_status=st, campaign=ccamp,
                                                     practice_group=c_by_npi[cr.npi])
                ctx = f"npi={cr.npi} status={st} campaign={cname}"
                self.assertEqual(cw.campaign, mw.campaign, ctx)
                self.assertEqual(cw.activation_priority, mw.activation_priority, ctx)
                self.assertEqual(cw.activation_score, mw.activation_score, ctx)
                self.assertEqual(cw.priority_reason, mw.priority_reason, ctx)
                self.assertEqual(cw.profile_completeness_score, mw.profile_completeness_score, ctx)
                self.assertEqual(cw.missing_profile_fields_joined, mw.missing_profile_fields, ctx)
                self.assertEqual(cw.practice_group_id, mw.practice_group_id, ctx)
                self.assertEqual(cw.practice_group_size, mw.practice_group_size, ctx)
                self.assertEqual(cw.other_doctors_joined, mw.other_doctors_at_location, ctx)
                checks += 1
        self.assertGreater(checks, 0)

    # ---- the big one: workflow fields across statuses x campaigns ----
    def test_workflow_fields_parity_live_csv(self):
        self._assert_workflow_parity(self.mono_records, self.core_records)

    def test_workflow_fields_parity_fixture(self):
        self._assert_workflow_parity(self.fix_mono, self.fix_core)

    # ---- priority_counts / average ----
    def test_aggregates(self):
        m = self.m
        cgroups = grouping.build_practice_groups(self.core_records)
        c_by_npi = grouping.group_by_npi(cgroups)
        mgroups = m._build_practice_groups(self.mono_records)
        m_by_npi = m._group_by_npi(mgroups)
        cws, mws = [], []
        for i, (mr, cr) in enumerate(zip(self.mono_records, self.core_records)):
            st = STATUSES[i % len(STATUSES)]
            mws.append(m._workflow_fields(mr, activation_status=st, campaign=None,
                                          practice_group=m_by_npi[mr.npi]))
            cws.append(scoring.compute_workflow_fields(cr, activation_status=st, campaign=None,
                                                       practice_group=c_by_npi[cr.npi]))
        self.assertEqual(scoring.priority_counts(cws), m._priority_counts(mws))
        self.assertEqual(scoring.average_profile_completeness(cws), m._average_profile_completeness(mws))

    # ---- queue ranking ----
    def test_rank_queue_parity(self):
        m = self.m
        cgroups = grouping.build_practice_groups(self.core_records)
        c_by_npi = grouping.group_by_npi(cgroups)
        mono_rows, core_items = [], []
        for i, cr in enumerate(self.core_records):
            st = STATUSES[i % len(STATUSES)]
            cw = scoring.compute_workflow_fields(cr, activation_status=st, campaign=None,
                                                 practice_group=c_by_npi[cr.npi])
            mono_rows.append({
                "npi": cr.npi,
                "doctor_name": cr.display_name,
                "activation_status": st,
                "activation_priority": cw.activation_priority,
                "activation_score": str(cw.activation_score),
            })
            core_items.append(queue.QueueItem(
                npi=cr.npi, doctor_name=cr.display_name, activation_status=st,
                activation_priority=cw.activation_priority, activation_score=cw.activation_score,
            ))
        mono_ranked = m._rox_today_rows(mono_rows)
        core_ranked = queue.rank_queue(core_items)
        self.assertEqual(
            [(int(r["queue_rank"]), r["npi"]) for r in mono_ranked],
            [(r.queue_rank, r.item.npi) for r in core_ranked],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
