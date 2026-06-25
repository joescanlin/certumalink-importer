"""template authoring metadata (Studio AI compose)

Adds three columns to template so the studio can author copy with a chosen model and split it across
message types: message_type (first_touch / follow_up_* / objection_reply / re_engage), model (the
authoring model id), and source (manual | ai). Pure additive with safe defaults so existing rows are
unaffected; downgrade drops the columns.

Revision ID: 0012_template_compose
Revises: 0011_support_tickets
Create Date: 2026-06-24
"""
from alembic import op

revision = "0012_template_compose"
down_revision = "0011_support_tickets"
branch_labels = None
depends_on = None


UPGRADE = [
    "ALTER TABLE template ADD COLUMN message_type TEXT NOT NULL DEFAULT 'first_touch';",
    "ALTER TABLE template ADD COLUMN model TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE template ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';",
    "CREATE INDEX ix_template_message_type ON template(message_type);",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_template_message_type;",
    "ALTER TABLE template DROP COLUMN IF EXISTS source;",
    "ALTER TABLE template DROP COLUMN IF EXISTS model;",
    "ALTER TABLE template DROP COLUMN IF EXISTS message_type;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
