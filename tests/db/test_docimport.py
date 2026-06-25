"""Document-import tests (terminal -> platform). Skips without DB.

Imports a generated doctors.csv from the workspace into Prospects (+ Leads). Isolates the workspace
to a temp dir so it never touches the live /tmp/certuma-docs.
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, inspect, select, text
    from sqlalchemy.orm import Session
    HAVE_SA = True
except Exception:  # pragma: no cover
    HAVE_SA = False

if HAVE_SA:
    from certuma import docimport, webterm
    from certuma.config import Settings
    from certuma.db.models import Campaign, Lead, Prospect

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")
_FIELDS = ["npi", "first_name", "last_name", "display_name", "primary_specialty",
           "practice_city", "practice_state"]


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed")
class DocImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable: run `make db-up migrate` ({exc})")
        if "prospect" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")
        with Session(cls.engine) as s:
            if s.get(Campaign, "primary-care") is None:
                raise unittest.SkipTest("campaign seed (0002) not applied: run `make migrate`")

    @classmethod
    def tearDownClass(cls):
        if HAVE_SA and getattr(cls, "engine", None) is not None:
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(self.engine)
        self._real_docs = webterm.DOCS_ROOT
        webterm.DOCS_ROOT = Path(tempfile.mkdtemp(prefix="certuma-docs-test-"))

    def tearDown(self):
        self.session.rollback()
        self.session.close()
        shutil.rmtree(webterm.DOCS_ROOT, ignore_errors=True)
        webterm.DOCS_ROOT = self._real_docs

    def _write_doc(self, rows, run="run-901-import", name="doctors.csv"):
        d = webterm.DOCS_ROOT / run
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in _FIELDS})
        return f"{run}/{name}"

    def test_import_creates_prospects_and_leads(self):
        rel = self._write_doc([
            {"npi": "3900000001", "first_name": "Ada", "last_name": "Lovelace",
             "display_name": "Ada Lovelace MD", "primary_specialty": "Cardiology",
             "practice_city": "Austin", "practice_state": "TX"},
            {"npi": "3900000002", "first_name": "Al", "last_name": "K", "display_name": "",
             "primary_specialty": "Dermatology", "practice_city": "Austin", "practice_state": "TX"},
            {"npi": "bad-npi", "first_name": "Nope"},  # skipped
        ])
        result = docimport.import_document(self.session, rel, campaign="primary-care")
        self.assertEqual(result.created, 2)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.leads_created, 2)
        p = self.session.get(Prospect, "3900000001")
        self.assertEqual(p.primary_specialty, "Cardiology")
        self.assertEqual(p.practice_state, "TX")
        # display_name falls back to the name when the column is blank
        self.assertEqual(self.session.get(Prospect, "3900000002").display_name, "Al K")
        lead = self.session.execute(select(Lead).where(Lead.npi == "3900000001")).scalar()
        self.assertEqual(lead.campaign, "primary-care")
        self.assertEqual(lead.activation_status, "not_contacted")

    def test_import_is_idempotent(self):
        rel = self._write_doc([{"npi": "3900000005", "first_name": "Re", "last_name": "Run",
                                "primary_specialty": "Pediatrics", "practice_state": "CA"}])
        docimport.import_document(self.session, rel, campaign="primary-care")
        again = docimport.import_document(self.session, rel, campaign="primary-care")
        self.assertEqual(again.created, 0)
        self.assertEqual(again.updated, 1)
        self.assertEqual(again.leads_created, 0)  # the lead already exists

    def test_prospects_only_when_no_campaign(self):
        rel = self._write_doc([{"npi": "3900000006", "first_name": "Solo", "last_name": "Prospect",
                                "primary_specialty": "Neurology", "practice_state": "NY"}])
        result = docimport.import_document(self.session, rel, campaign=None)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.leads_created, 0)
        self.assertIsNotNone(self.session.get(Prospect, "3900000006"))
        self.assertIsNone(self.session.execute(
            select(Lead).where(Lead.npi == "3900000006")).scalar())

    def test_unknown_campaign_rejected(self):
        rel = self._write_doc([{"npi": "3900000007", "first_name": "X", "last_name": "Y"}])
        with self.assertRaises(ValueError):
            docimport.import_document(self.session, rel, campaign="no-such-campaign")

    def test_non_csv_or_missing_npi_rejected(self):
        d = webterm.DOCS_ROOT / "run-902-bad"
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            docimport.import_document(self.session, "run-902-bad/summary.json")
        (d / "no_npi.csv").write_text("name,city\nAda,Austin\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            docimport.import_document(self.session, "run-902-bad/no_npi.csv")

    def test_import_refuses_paths_outside_the_workspace(self):
        with self.assertRaises(ValueError):
            docimport.import_document(self.session, "../../etc/passwd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
