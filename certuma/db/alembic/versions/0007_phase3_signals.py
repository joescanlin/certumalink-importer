"""phase 3 clinician knowledge graph / signals (task P3.3)

A per-clinician signal store - the "knowledge graph + Clever Columns" the proposal sells. Each row
is one observed signal (license, specialty board, region, group size, message-burden, public
activity, and EHR / panel-size behind the vendor seam) with its source, confidence, and observed_at
so scoring can apply recency decay (latent issue 4). One current value per (npi, signal_type,
source) via a unique index, so providers upsert. Pure additive; downgrade drops the table.

Revision ID: 0007_phase3_signals
Revises: 0006_phase3_reporting
Create Date: 2026-06-24
"""
from alembic import op

revision = "0007_phase3_signals"
down_revision = "0006_phase3_reporting"
branch_labels = None
depends_on = None


UPGRADE = [
    """
    CREATE TABLE clinician_signal (
        id             BIGSERIAL PRIMARY KEY,
        npi            VARCHAR(10) NOT NULL REFERENCES prospect(npi),
        signal_type    TEXT NOT NULL,
        value          TEXT,
        numeric_value  NUMERIC,
        source         TEXT NOT NULL DEFAULT '',
        confidence     NUMERIC(4,3) NOT NULL DEFAULT 1.0,
        observed_at    TIMESTAMPTZ NOT NULL,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE UNIQUE INDEX uq_signal_npi_type_source ON clinician_signal(npi, signal_type, source);",
    "CREATE INDEX ix_signal_npi ON clinician_signal(npi);",
    "CREATE INDEX ix_signal_type ON clinician_signal(signal_type);",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS clinician_signal;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
