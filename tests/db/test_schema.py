"""Schema + constraint tests against a migrated Postgres (Phase 0 tasks B7, §8-F/§8-D subset).

Skips cleanly when no DB is reachable (so the default test run elsewhere stays green).
To run: `make db-up migrate` then `make test-db`  (or PYTHONPATH=.:src .venv/bin/python -m
unittest discover -s tests/db).

Every constraint test runs inside a rolled-back transaction, so the DB is never mutated.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import DBAPIError, IntegrityError
    HAVE_SA = True
except Exception:  # pragma: no cover - sqlalchemy not installed in this interpreter
    HAVE_SA = False

if HAVE_SA:
    from certuma.config import Settings
    from certuma.db import models
    from certuma.db.base import Base

DB_URL = os.environ.get("CERTUMA_DATABASE_URL") or (Settings().database_url if HAVE_SA else "")


@unittest.skipUnless(HAVE_SA, "SQLAlchemy not installed in this interpreter")
class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DB_URL, future=True)
        try:
            with cls.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres unreachable at {DB_URL}: run `make db-up migrate` ({exc})")
        if "lead" not in inspect(cls.engine).get_table_names():
            raise unittest.SkipTest("schema not migrated: run `make migrate`")

    def setUp(self):
        self.conn = self.engine.connect()
        self.trans = self.conn.begin()

    def tearDown(self):
        self.trans.rollback()
        self.conn.close()

    def _seed_lead(self):
        self.conn.execute(text("INSERT INTO campaign(name,label) VALUES ('t','T')"))
        self.conn.execute(text("INSERT INTO prospect(npi) VALUES ('1000000001')"))
        return self.conn.execute(
            text("INSERT INTO lead(npi,campaign) VALUES ('1000000001','t') RETURNING id")
        ).scalar_one()

    # ---- structure ----
    def test_all_15_tables_present(self):
        names = set(inspect(self.engine).get_table_names())
        for table in models.ALL_TABLES:
            self.assertIn(table, names)
        self.assertTrue(set(models.ALL_TABLES) <= names)

    def test_orm_columns_exist_in_db(self):
        """Drift guard: every column declared on an ORM model exists in the migrated DB."""
        insp = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            db_cols = {c["name"] for c in insp.get_columns(table.name)}
            orm_cols = {c.name for c in table.columns}
            missing = orm_cols - db_cols
            self.assertFalse(missing, msg=f"{table.name}: ORM columns missing from DB: {missing}")

    def test_citext_extension(self):
        self.assertEqual(
            self.conn.execute(text("SELECT extname FROM pg_extension WHERE extname='citext'")).scalar(),
            "citext",
        )

    def test_critical_indexes_present(self):
        rows = self.conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname = ANY(:names)"
        ), {"names": [
            "uq_msg_idem_outbound", "uq_msg_inbound_esp", "uq_event_dedup",
            "uq_suppress_npi", "uq_suppress_email",
        ]}).scalars().all()
        self.assertEqual(set(rows), {
            "uq_msg_idem_outbound", "uq_msg_inbound_esp", "uq_event_dedup",
            "uq_suppress_npi", "uq_suppress_email",
        })

    # ---- constraints ----
    def test_lead_status_check_rejects_offlist(self):
        self._seed_lead()
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(text(
                    "INSERT INTO lead(npi,campaign,activation_status) "
                    "VALUES ('1000000001','t','bogus_status')"
                ))

    def test_lead_campaign_fk_rejects_unknown(self):
        self.conn.execute(text("INSERT INTO prospect(npi) VALUES ('1000000002')"))
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(text(
                    "INSERT INTO lead(npi,campaign) VALUES ('1000000002','no_such_campaign')"
                ))

    def test_idempotency_outbound_unique(self):
        lead_id = self._seed_lead()
        ins = text(
            "INSERT INTO message(lead_id,npi,campaign,cadence_step,direction) "
            "VALUES (:lid,'1000000001','t',1,'outbound')"
        )
        self.conn.execute(ins, {"lid": lead_id})
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(ins, {"lid": lead_id})

    def test_inbound_esp_dedup_but_distinct_ok(self):
        lead_id = self._seed_lead()
        ins = text(
            "INSERT INTO message(lead_id,npi,campaign,cadence_step,direction,esp_message_id) "
            "VALUES (:lid,'1000000001','t',:step,'inbound',:esp)"
        )
        # distinct esp ids -> both fine (no false collision with the outbound idem key)
        self.conn.execute(ins, {"lid": lead_id, "step": 0, "esp": "esp-A"})
        self.conn.execute(ins, {"lid": lead_id, "step": 0, "esp": "esp-B"})
        # duplicate esp id -> collide
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(ins, {"lid": lead_id, "step": 0, "esp": "esp-A"})

    def test_suppression_unique_npi_and_email(self):
        self.conn.execute(text("INSERT INTO suppression(npi,reason) VALUES ('1000000003','opt_out')"))
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(text("INSERT INTO suppression(npi,reason) VALUES ('1000000003','complaint')"))
        self.conn.execute(text("INSERT INTO suppression(email,reason) VALUES ('a@x.com','opt_out')"))
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(text("INSERT INTO suppression(email,reason) VALUES ('A@X.COM','complaint')"))

    def test_event_dedup_unique(self):
        ins = text(
            "INSERT INTO event(dedup_key,event_type,occurred_at) VALUES (:k,'activated',now())"
        )
        self.conn.execute(ins, {"k": "poll:activated:1000000001:t"})
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(ins, {"k": "poll:activated:1000000001:t"})

    def test_kill_switch_singleton(self):
        self.assertEqual(self.conn.execute(text("SELECT count(*) FROM kill_switch")).scalar(), 1)
        with self.assertRaises((IntegrityError, DBAPIError)):
            with self.conn.begin_nested():
                self.conn.execute(text("INSERT INTO kill_switch(id,is_active) VALUES (2,false)"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
