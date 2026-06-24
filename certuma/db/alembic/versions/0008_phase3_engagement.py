"""phase 3 engagement rollup (task P3.5 open tracking)

Adds a per-lead engagement rollup (open_count, last_open_at) the monitor updates on an 'opened'
event, so the engagement plays (P3.6: opened-no-reply, went-quiet) and the dashboard can act on
interest without scanning the event log. The 'opened' event_type already exists (0001); opens are a
WEAK signal (Apple Mail Privacy Protection inflates them), so these never hard-trigger a high-stakes
action. Pure additive; downgrade reverses.

Revision ID: 0008_phase3_engagement
Revises: 0007_phase3_signals
Create Date: 2026-06-24
"""
from alembic import op

revision = "0008_phase3_engagement"
down_revision = "0007_phase3_signals"
branch_labels = None
depends_on = None


UPGRADE = [
    "ALTER TABLE lead ADD COLUMN open_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE lead ADD COLUMN last_open_at TIMESTAMPTZ;",
    "ALTER TABLE lead ADD COLUMN last_engaged_at TIMESTAMPTZ;",  # any inbound signal (open or reply)
]

DOWNGRADE = [
    "ALTER TABLE lead DROP COLUMN IF EXISTS last_engaged_at;",
    "ALTER TABLE lead DROP COLUMN IF EXISTS last_open_at;",
    "ALTER TABLE lead DROP COLUMN IF EXISTS open_count;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
