"""customer-support agents (Phase 4 / support)

Adds support_ticket: an inbound onboarding/support message from an (often activated) physician, which
the support agent classifies, answers or escalates, AND turns into a SALES signal written to the
shared clinician_signal knowledge graph (expansion_intent / advocate / churn_risk_support) - so the
support loop enriches sales intelligence. Pure additive; downgrade drops the table.

Revision ID: 0011_support_tickets
Revises: 0010_phase3_auth
Create Date: 2026-06-24
"""
from alembic import op

revision = "0011_support_tickets"
down_revision = "0010_phase3_auth"
branch_labels = None
depends_on = None


UPGRADE = [
    """
    CREATE TABLE support_ticket (
        id              BIGSERIAL PRIMARY KEY,
        npi             VARCHAR(10) REFERENCES prospect(npi),
        channel         TEXT NOT NULL DEFAULT 'portal',
        subject         TEXT,
        body            TEXT NOT NULL,
        intent          TEXT,
        status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','answered','escalated','resolved')),
        answer          TEXT,
        emitted_signal  TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at     TIMESTAMPTZ
    );
    """,
    "CREATE INDEX ix_support_status ON support_ticket(status);",
    "CREATE INDEX ix_support_npi ON support_ticket(npi);",
    "CREATE INDEX ix_support_open ON support_ticket(id) WHERE intent IS NULL;",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS support_ticket;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
