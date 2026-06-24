"""phase 3 auth + RBAC (task P3.9)

Adds console_user (dashboard login with a salted PBKDF2 password hash and a role:
operator / leadership / admin) and access_log (auth events + mutations, toward a SOC 2 posture).
Operational truth is untouched; these only gate and audit who reaches the dashboard. Downgrade drops
both tables.

Revision ID: 0010_phase3_auth
Revises: 0009_phase3_channels
Create Date: 2026-06-24
"""
from alembic import op

revision = "0010_phase3_auth"
down_revision = "0009_phase3_channels"
branch_labels = None
depends_on = None


UPGRADE = [
    """
    CREATE TABLE console_user (
        id             BIGSERIAL PRIMARY KEY,
        username       CITEXT UNIQUE NOT NULL,
        password_hash  TEXT NOT NULL,
        salt           TEXT NOT NULL,
        role           TEXT NOT NULL CHECK (role IN ('operator','leadership','admin')),
        is_active      BOOLEAN NOT NULL DEFAULT true,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE access_log (
        id        BIGSERIAL PRIMARY KEY,
        username  TEXT,
        role      TEXT,
        action    TEXT NOT NULL,
        path      TEXT,
        at        TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_access_log_at ON access_log(at);",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS access_log;",
    "DROP TABLE IF EXISTS console_user;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
