"""initial Certuma schema (15 tables)

Authoritative DDL, matching docs/certuma-architecture.md §3. Executed in dependency order:
citext extension first, then tables, with the prospect<->practice_group cycle broken by a
post-hoc ALTER. Idempotency is a partial unique index scoped to outbound messages; event
dedup and suppression(email,npi) are enforced by unique indexes.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-23
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = [
    # ---- extension first (CI asserts present before any table) ----
    "CREATE EXTENSION IF NOT EXISTS citext;",

    # ---- practice_group (no FK out) ----
    """
    CREATE TABLE practice_group (
        practice_group_id   TEXT PRIMARY KEY,
        practice_group_size INTEGER NOT NULL DEFAULT 0,
        practice_phone      TEXT NOT NULL DEFAULT '',
        practice_address_1  TEXT NOT NULL DEFAULT '',
        practice_address_2  TEXT NOT NULL DEFAULT '',
        practice_city       TEXT NOT NULL DEFAULT '',
        practice_state      VARCHAR(2) NOT NULL DEFAULT '',
        practice_zip        TEXT NOT NULL DEFAULT '',
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_pgroup_size ON practice_group(practice_group_size);",

    # ---- app_user ----
    """
    CREATE TABLE app_user (
        id          BIGSERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        email       CITEXT UNIQUE,
        role        TEXT NOT NULL CHECK (role IN ('owner','backup','system')),
        is_active   BOOLEAN NOT NULL DEFAULT true,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,

    # ---- campaign ----
    """
    CREATE TABLE campaign (
        name            TEXT PRIMARY KEY,
        label           TEXT NOT NULL,
        specialty_terms TEXT[] NOT NULL DEFAULT '{}',
        priority_boost  INTEGER NOT NULL DEFAULT 0,
        pitch_angle     TEXT NOT NULL DEFAULT '',
        autonomy_level  TEXT NOT NULL DEFAULT 'assisted'
                        CHECK (autonomy_level IN ('assisted','supervised','autonomous')),
        is_active       BOOLEAN NOT NULL DEFAULT false,
        is_paused       BOOLEAN NOT NULL DEFAULT false,
        version         INTEGER NOT NULL DEFAULT 1,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,

    # ---- prospect (FK to practice_group added after both exist) ----
    """
    CREATE TABLE prospect (
        npi                   VARCHAR(10) PRIMARY KEY,
        first_name            TEXT NOT NULL DEFAULT '',
        middle_name           TEXT NOT NULL DEFAULT '',
        last_name             TEXT NOT NULL DEFAULT '',
        credential            TEXT NOT NULL DEFAULT '',
        display_name          TEXT NOT NULL DEFAULT '',
        primary_taxonomy_code TEXT NOT NULL DEFAULT '',
        primary_specialty     TEXT NOT NULL DEFAULT '',
        practice_address_1    TEXT NOT NULL DEFAULT '',
        practice_address_2    TEXT NOT NULL DEFAULT '',
        practice_city         TEXT NOT NULL DEFAULT '',
        practice_state        VARCHAR(2) NOT NULL DEFAULT '',
        practice_zip          TEXT NOT NULL DEFAULT '',
        practice_phone        TEXT NOT NULL DEFAULT '',
        matched_zips          TEXT[] NOT NULL DEFAULT '{}',
        source                TEXT NOT NULL DEFAULT 'cms_nppes_registry_api',
        source_fetched_at     TIMESTAMPTZ,
        practice_group_id     TEXT,
        profile_url           TEXT,
        profile_slug          TEXT,
        created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    ALTER TABLE prospect
        ADD CONSTRAINT fk_prospect_group
        FOREIGN KEY (practice_group_id) REFERENCES practice_group(practice_group_id);
    """,
    "CREATE INDEX ix_prospect_group ON prospect(practice_group_id);",
    "CREATE INDEX ix_prospect_state ON prospect(practice_state);",
    "CREATE INDEX ix_prospect_zip   ON prospect(practice_zip);",

    # ---- contact ----
    """
    CREATE TABLE contact (
        id              BIGSERIAL PRIMARY KEY,
        npi             VARCHAR(10) NOT NULL REFERENCES prospect(npi),
        email           CITEXT,
        email_status    TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (email_status IN ('valid','risky','catch_all','unknown','invalid')),
        verifier        TEXT,
        verified_at     TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (npi, email)
    );
    """,
    "CREATE INDEX ix_contact_npi    ON contact(npi);",
    "CREATE INDEX ix_contact_status ON contact(email_status);",

    # ---- workflow_score (time-series) ----
    """
    CREATE TABLE workflow_score (
        id                         BIGSERIAL PRIMARY KEY,
        npi                        VARCHAR(10) NOT NULL REFERENCES prospect(npi),
        campaign                   TEXT NOT NULL DEFAULT '' REFERENCES campaign(name),
        activation_priority        TEXT NOT NULL CHECK (activation_priority IN ('high','medium','low')),
        activation_score           INTEGER NOT NULL,
        priority_reason            TEXT NOT NULL DEFAULT '',
        full_priority_reasons      TEXT[] NOT NULL DEFAULT '{}',
        profile_completeness_score INTEGER NOT NULL,
        missing_profile_fields     TEXT[] NOT NULL DEFAULT '{}',
        practice_group_id          TEXT,
        practice_group_size        INTEGER NOT NULL DEFAULT 0,
        model_version              TEXT NOT NULL,
        scored_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_wscore_npi_time ON workflow_score(npi, scored_at DESC);",
    "CREATE INDEX ix_wscore_campaign ON workflow_score(campaign);",

    # ---- lead (live state machine) ----
    """
    CREATE TABLE lead (
        id                     BIGSERIAL PRIMARY KEY,
        npi                    VARCHAR(10) NOT NULL REFERENCES prospect(npi),
        campaign               TEXT NOT NULL REFERENCES campaign(name),
        activation_status      TEXT NOT NULL DEFAULT 'not_contacted',
        cadence_step           INTEGER NOT NULL DEFAULT 0,
        next_action_at         TIMESTAMPTZ,
        stop_reason            TEXT,
        owner                  TEXT,
        claim_url              TEXT,
        last_polled_at         TIMESTAMPTZ,
        activation_detected_at TIMESTAMPTZ,
        version                INTEGER NOT NULL DEFAULT 0,
        last_seen_at           TIMESTAMPTZ,
        created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT lead_status_valid CHECK (activation_status IN (
            'not_contacted','queued_today','enriching','sendable','email_sent',
            'awaiting_reply','replied','interested','called_no_answer','voicemail_left',
            'physician_activated','do_not_contact','needs_review','exhausted')),
        UNIQUE (npi, campaign)
    );
    """,
    "CREATE INDEX ix_lead_status      ON lead(activation_status);",
    "CREATE INDEX ix_lead_next_action ON lead(next_action_at) WHERE next_action_at IS NOT NULL;",
    """
    CREATE INDEX ix_lead_poll ON lead(last_polled_at)
        WHERE activation_status NOT IN ('physician_activated','do_not_contact','exhausted');
    """,

    # ---- thread ----
    """
    CREATE TABLE thread (
        id              BIGSERIAL PRIMARY KEY,
        lead_id         BIGINT NOT NULL REFERENCES lead(id),
        reply_token     TEXT UNIQUE,
        is_locked       BOOLEAN NOT NULL DEFAULT false,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_thread_lead ON thread(lead_id);",

    # ---- message (idempotency key written before the ESP call) ----
    """
    CREATE TABLE message (
        id              BIGSERIAL PRIMARY KEY,
        lead_id         BIGINT NOT NULL REFERENCES lead(id),
        thread_id       BIGINT REFERENCES thread(id),
        npi             VARCHAR(10) NOT NULL,
        campaign        TEXT NOT NULL REFERENCES campaign(name),
        cadence_step    INTEGER NOT NULL,
        direction       TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
        variant_id      TEXT,
        subject         TEXT,
        body_rendered   TEXT,
        esp_message_id  TEXT,
        sent_at         TIMESTAMPTZ,
        delivered       BOOLEAN NOT NULL DEFAULT false,
        bounced         BOOLEAN NOT NULL DEFAULT false,
        complained      BOOLEAN NOT NULL DEFAULT false,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE UNIQUE INDEX uq_msg_idem_outbound
        ON message (npi, campaign, cadence_step)
        WHERE direction = 'outbound';
    """,
    """
    CREATE UNIQUE INDEX uq_msg_inbound_esp
        ON message (esp_message_id)
        WHERE direction = 'inbound' AND esp_message_id IS NOT NULL;
    """,
    "CREATE INDEX ix_msg_lead   ON message(lead_id);",
    "CREATE INDEX ix_msg_thread ON message(thread_id);",

    # ---- event (deduped incl. polled/internal) ----
    """
    CREATE TABLE event (
        id              BIGSERIAL PRIMARY KEY,
        dedup_key       TEXT NOT NULL,
        lead_id         BIGINT REFERENCES lead(id),
        message_id      BIGINT REFERENCES message(id),
        npi             VARCHAR(10),
        event_type      TEXT NOT NULL CHECK (event_type IN
                        ('delivered','opened','replied','bounced','complained',
                         'activated','opt_out','unsubscribe_click','sent')),
        payload         JSONB NOT NULL DEFAULT '{}',
        occurred_at     TIMESTAMPTZ NOT NULL,
        ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE UNIQUE INDEX uq_event_dedup ON event(dedup_key);",
    "CREATE INDEX ix_event_lead ON event(lead_id);",
    "CREATE INDEX ix_event_type ON event(event_type);",

    # ---- suppression (keyed by BOTH email AND npi) ----
    """
    CREATE TABLE suppression (
        id              BIGSERIAL PRIMARY KEY,
        npi             VARCHAR(10),
        email           CITEXT,
        reason          TEXT NOT NULL CHECK (reason IN
                        ('opt_out','hard_bounce','complaint','do_not_contact','manual','legal')),
        source          TEXT NOT NULL DEFAULT 'system',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT suppression_has_key CHECK (npi IS NOT NULL OR email IS NOT NULL)
    );
    """,
    "CREATE UNIQUE INDEX uq_suppress_npi   ON suppression(npi)   WHERE npi   IS NOT NULL;",
    "CREATE UNIQUE INDEX uq_suppress_email ON suppression(email) WHERE email IS NOT NULL;",

    # ---- template ----
    """
    CREATE TABLE template (
        id              BIGSERIAL PRIMARY KEY,
        campaign        TEXT REFERENCES campaign(name),
        version         INTEGER NOT NULL DEFAULT 1,
        subject         TEXT NOT NULL,
        body            TEXT NOT NULL,
        merge_tokens    TEXT[] NOT NULL DEFAULT '{}',
        is_approved     BOOLEAN NOT NULL DEFAULT false,
        approved_by     TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (campaign, version)
    );
    """,

    # ---- approval ----
    """
    CREATE TABLE approval (
        id               BIGSERIAL PRIMARY KEY,
        lead_id          BIGINT NOT NULL REFERENCES lead(id),
        proposed_action  TEXT NOT NULL,
        value_tier       TEXT,
        model_confidence NUMERIC(4,3),
        gate_reason_code TEXT,
        proposed_subject TEXT,
        proposed_body    TEXT,
        state            TEXT NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending','approved','rejected','edited','expired')),
        sla_expires_at   TIMESTAMPTZ,
        decided_by       BIGINT REFERENCES app_user(id),
        decided_at       TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_approval_state ON approval(state);",

    # ---- audit_log (append-only) ----
    """
    CREATE TABLE audit_log (
        id              BIGSERIAL PRIMARY KEY,
        entity          TEXT NOT NULL,
        entity_id       TEXT NOT NULL,
        npi             VARCHAR(10),
        action          TEXT NOT NULL,
        old_value       JSONB,
        new_value       JSONB,
        actor           TEXT NOT NULL,
        reason_code     TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);",
    "CREATE INDEX ix_audit_npi    ON audit_log(npi);",

    # ---- kill_switch (singleton) ----
    """
    CREATE TABLE kill_switch (
        id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        is_active   BOOLEAN NOT NULL DEFAULT false,
        set_by      BIGINT REFERENCES app_user(id),
        set_at      TIMESTAMPTZ
    );
    """,
    "INSERT INTO kill_switch (id, is_active) VALUES (1, false);",
]

# reverse dependency order; CASCADE covers any residual FK edges.
DROP_TABLES = [
    "kill_switch", "audit_log", "approval", "template", "suppression", "event",
    "message", "thread", "lead", "workflow_score", "contact", "prospect",
    "campaign", "app_user", "practice_group",
]


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table in DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    op.execute("DROP EXTENSION IF EXISTS citext;")
