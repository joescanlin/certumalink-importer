"""phase 2 inbound replies (tasks P2.1 / P2.2)

Additive columns so an inbound reply can be stored, linked to the outbound it answers, and tagged
with the classifier's intent. The inbound-dedup unique index (uq_msg_inbound_esp) and the
direction CHECK already exist from 0001; this only adds the reply linkage + classification fields
and a partial index for the classifier to find unclassified inbound messages. Downgrade reverses.

Revision ID: 0004_phase2_replies
Revises: 0003_phase1
Create Date: 2026-06-23
"""
from alembic import op

revision = "0004_phase2_replies"
down_revision = "0003_phase1"
branch_labels = None
depends_on = None


UPGRADE = [
    # the intent the reply classifier assigned to an inbound message (NULL until classified)
    "ALTER TABLE message ADD COLUMN reply_classification TEXT;",
    # link an inbound reply to the outbound message it answers (best-effort thread linkage)
    "ALTER TABLE message ADD COLUMN in_reply_to BIGINT REFERENCES message(id);",
    # the classifier worker scans for inbound messages it has not labelled yet
    """
    CREATE INDEX ix_message_unclassified ON message(lead_id)
        WHERE direction = 'inbound' AND reply_classification IS NULL;
    """,
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_message_unclassified;",
    "ALTER TABLE message DROP COLUMN IF EXISTS in_reply_to;",
    "ALTER TABLE message DROP COLUMN IF EXISTS reply_classification;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
