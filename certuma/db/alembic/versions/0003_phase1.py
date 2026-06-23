"""phase 1 send-loop tables and columns (task P1.1)

Adds the mailbox roster (cold-domain sending accounts; ships empty, one dev mailbox for Mailpit),
the persisted circuit_breaker_state (the Gate reads it; the ingest side trips it), and the columns
the sender / enrichment / template-approval flows need. Pure additive; downgrade reverses it.

NOTE: the matching certuma_core/status.py ALLOWED_TRANSITIONS edges (email_sent/awaiting_reply ->
interested) are a code change shipped alongside this migration (no DDL - the lead status CHECK
already lists 'interested').

Revision ID: 0003_phase1
Revises: 0002_seed
Create Date: 2026-06-23
"""
from alembic import op

revision = "0003_phase1"
down_revision = "0002_seed"
branch_labels = None
depends_on = None


UPGRADE = [
    # --- mailbox roster (cold-domain sending accounts) ---
    """
    CREATE TABLE mailbox (
        id            BIGSERIAL PRIMARY KEY,
        address       CITEXT UNIQUE NOT NULL,        -- from address, e.g. jordan@getcertuma.com
        display_name  TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        domain        TEXT NOT NULL DEFAULT '',
        daily_cap     INTEGER NOT NULL DEFAULT 50,   -- warmup cap, sends/day
        is_active     BOOLEAN NOT NULL DEFAULT true,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    # --- circuit breaker state (read by the Gate, tripped by ingest) ---
    """
    CREATE TABLE circuit_breaker_state (
        id            BIGSERIAL PRIMARY KEY,
        scope         TEXT NOT NULL,                 -- e.g. 'global', 'campaign:dermatology'
        breaker       TEXT NOT NULL CHECK (breaker IN ('complaint','bounce')),
        is_tripped    BOOLEAN NOT NULL DEFAULT false,
        rate          NUMERIC(6,4) NOT NULL DEFAULT 0,
        sample_count  INTEGER NOT NULL DEFAULT 0,
        tripped_at    TIMESTAMPTZ,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (scope, breaker)
    );
    """,
    "CREATE INDEX ix_breaker_tripped ON circuit_breaker_state(scope) WHERE is_tripped;",

    # --- message: which mailbox sent it (nullable; required by the SENDER idempotency dict) ---
    "ALTER TABLE message ADD COLUMN mailbox_id BIGINT REFERENCES mailbox(id);",
    """
    CREATE INDEX ix_msg_esp_message_id ON message(esp_message_id)
        WHERE direction = 'outbound' AND esp_message_id IS NOT NULL;
    """,

    # --- lead: re-enrich flag (set on hard bounce) ---
    "ALTER TABLE lead ADD COLUMN needs_reenrich BOOLEAN NOT NULL DEFAULT false;",

    # --- template: authoring + A/B variant metadata ---
    "ALTER TABLE template ADD COLUMN created_by TEXT;",
    "ALTER TABLE template ADD COLUMN variant_label TEXT NOT NULL DEFAULT '';",
    "CREATE INDEX ix_template_active ON template(campaign) WHERE is_approved;",

    # --- contact: enrichment role-address + provenance ---
    "ALTER TABLE contact ADD COLUMN is_role_address BOOLEAN NOT NULL DEFAULT false;",
    "ALTER TABLE contact ADD COLUMN discovery_source TEXT;",

    # --- event: time-window queries for the circuit breakers ---
    "CREATE INDEX ix_event_occurred_at ON event(occurred_at);",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS ix_event_occurred_at;",
    "ALTER TABLE contact DROP COLUMN IF EXISTS discovery_source;",
    "ALTER TABLE contact DROP COLUMN IF EXISTS is_role_address;",
    "DROP INDEX IF EXISTS ix_template_active;",
    "ALTER TABLE template DROP COLUMN IF EXISTS variant_label;",
    "ALTER TABLE template DROP COLUMN IF EXISTS created_by;",
    "ALTER TABLE lead DROP COLUMN IF EXISTS needs_reenrich;",
    "DROP INDEX IF EXISTS ix_msg_esp_message_id;",
    "ALTER TABLE message DROP COLUMN IF EXISTS mailbox_id;",
    "DROP TABLE IF EXISTS circuit_breaker_state;",
    "DROP TABLE IF EXISTS mailbox;",
]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
