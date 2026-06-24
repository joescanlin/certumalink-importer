"""phase 2 agent registry (Agent Studio)

Stores the editable LLM-agent configs (role, model, system prompt) so the copywriter / classifier /
reply-drafter prompts can be tuned and versioned from the dashboard instead of living only in code.
One active agent per role (partial unique index). Pure additive; the providers fall back to their
in-code default prompt when no row exists. Downgrade drops the table.

Revision ID: 0005_phase2_agents
Revises: 0004_phase2_replies
Create Date: 2026-06-23
"""
from alembic import op

revision = "0005_phase2_agents"
down_revision = "0004_phase2_replies"
branch_labels = None
depends_on = None


UPGRADE = [
    """
    CREATE TABLE agent (
        id             BIGSERIAL PRIMARY KEY,
        role           TEXT NOT NULL,              -- 'copywriter' | 'classifier' | 'reply_drafter'
        name           TEXT NOT NULL,
        model          TEXT NOT NULL DEFAULT '',
        system_prompt  TEXT NOT NULL,
        is_active      BOOLEAN NOT NULL DEFAULT false,
        version        INTEGER NOT NULL DEFAULT 1,
        created_by     TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE UNIQUE INDEX uq_agent_active_role ON agent(role) WHERE is_active;",  # one active per role
    "CREATE INDEX ix_agent_role ON agent(role);",
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS agent;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
