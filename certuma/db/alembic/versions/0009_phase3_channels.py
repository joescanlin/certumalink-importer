"""phase 3 multi-channel (task P3.8)

Adds message.channel so a touch records which channel it went out on (email today, linkedin behind
the stub seam). The reporting fact_touch already carries a channel column; the ELT now reads this one
instead of hardcoding 'email'. Pure additive; existing rows default to 'email'. Downgrade reverses.

Revision ID: 0009_phase3_channels
Revises: 0008_phase3_engagement
Create Date: 2026-06-24
"""
from alembic import op

revision = "0009_phase3_channels"
down_revision = "0008_phase3_engagement"
branch_labels = None
depends_on = None


UPGRADE = [
    "ALTER TABLE message ADD COLUMN channel TEXT NOT NULL DEFAULT 'email';",
    "CREATE INDEX ix_message_channel ON message(channel);",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_message_channel;",
    "ALTER TABLE message DROP COLUMN IF EXISTS channel;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
