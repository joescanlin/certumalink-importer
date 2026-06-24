"""phase 3 analytics reporting schema (task P3.0)

A separate, read-optimized `reporting` schema of conformed facts + dimensions, materialized from the
operational tables by the ELT (certuma/reporting/elt.py). It is NEVER a second writer of operational
state - it is rebuildable from scratch, so this migration only owns the DDL. Suppression flags are
carried so the analytics + evidence export inherit opt-out (latent issue 2: warehouse PII
governance). Downgrade drops the whole schema.

Revision ID: 0006_phase3_reporting
Revises: 0005_phase2_agents
Create Date: 2026-06-24
"""
from alembic import op

revision = "0006_phase3_reporting"
down_revision = "0005_phase2_agents"
branch_labels = None
depends_on = None


UPGRADE = [
    "CREATE SCHEMA IF NOT EXISTS reporting;",
    """
    CREATE TABLE reporting.dim_clinician (
        npi                TEXT PRIMARY KEY,
        display_name       TEXT,
        specialty          TEXT,
        state              TEXT,
        city               TEXT,
        practice_group_id  TEXT,
        group_size         INTEGER NOT NULL DEFAULT 0,
        is_suppressed      BOOLEAN NOT NULL DEFAULT false
    );
    """,
    """
    CREATE TABLE reporting.dim_campaign (
        campaign        TEXT PRIMARY KEY,
        label           TEXT,
        autonomy_level  TEXT,
        is_active       BOOLEAN NOT NULL DEFAULT false
    );
    """,
    """
    CREATE TABLE reporting.fact_touch (
        message_id    BIGINT PRIMARY KEY,
        npi           TEXT,
        campaign      TEXT,
        specialty     TEXT,
        state         TEXT,
        channel       TEXT NOT NULL DEFAULT 'email',
        variant_id    TEXT,
        cadence_step  INTEGER,
        sent_at       TIMESTAMPTZ,
        sent_date     DATE,
        delivered     BOOLEAN NOT NULL DEFAULT false,
        bounced       BOOLEAN NOT NULL DEFAULT false,
        send_cost     NUMERIC(8,4) NOT NULL DEFAULT 0
    );
    """,
    "CREATE INDEX ix_fact_touch_specialty ON reporting.fact_touch(specialty);",
    "CREATE INDEX ix_fact_touch_campaign ON reporting.fact_touch(campaign);",
    """
    CREATE TABLE reporting.fact_event (
        event_id       BIGINT PRIMARY KEY,
        npi            TEXT,
        campaign       TEXT,
        event_type     TEXT,
        occurred_at    TIMESTAMPTZ,
        occurred_date  DATE
    );
    """,
    "CREATE INDEX ix_fact_event_type ON reporting.fact_event(event_type);",
    """
    CREATE TABLE reporting.fact_lead_funnel (
        lead_id            BIGINT PRIMARY KEY,
        npi                TEXT,
        campaign           TEXT,
        specialty          TEXT,
        state              TEXT,
        activation_status  TEXT,
        cadence_step       INTEGER,
        has_contact        BOOLEAN NOT NULL DEFAULT false,
        sent               BOOLEAN NOT NULL DEFAULT false,
        delivered          BOOLEAN NOT NULL DEFAULT false,
        opened             BOOLEAN NOT NULL DEFAULT false,
        replied            BOOLEAN NOT NULL DEFAULT false,
        activated          BOOLEAN NOT NULL DEFAULT false,
        is_suppressed      BOOLEAN NOT NULL DEFAULT false,
        first_sent_at      TIMESTAMPTZ,
        activated_at       TIMESTAMPTZ
    );
    """,
    "CREATE INDEX ix_fact_funnel_specialty ON reporting.fact_lead_funnel(specialty);",
    "CREATE INDEX ix_fact_funnel_campaign ON reporting.fact_lead_funnel(campaign);",
    """
    CREATE TABLE reporting.meta (
        id          INTEGER PRIMARY KEY DEFAULT 1,
        rebuilt_at  TIMESTAMPTZ,
        as_of       TIMESTAMPTZ,
        CONSTRAINT reporting_meta_singleton CHECK (id = 1)
    );
    """,
]

DOWNGRADE = [
    "DROP SCHEMA IF EXISTS reporting CASCADE;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
