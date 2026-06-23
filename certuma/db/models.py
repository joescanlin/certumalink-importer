"""ORM models for the 15 Certuma tables.

These MAP onto the schema created by versions/0001_initial_schema.py (the authoritative DDL,
matching docs/certuma-architecture §3). create_all is never used in production; the migration
owns DDL. A reflection drift-guard test (tests/db) asserts every table/column here exists in
the migrated DB. citext columns are typed Text here (the DB enforces case-insensitivity).

Classic Column style (not Mapped[]) is used deliberately: this targets Python 3.9, where
SQLAlchemy cannot resolve PEP 604 `X | None` Mapped annotations.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from .base import Base

# Canonical state list (mirrors certuma_core.status.STATES; the DB CHECK is authoritative).
LEAD_STATES = (
    "not_contacted", "queued_today", "enriching", "sendable", "email_sent",
    "awaiting_reply", "replied", "interested", "called_no_answer", "voicemail_left",
    "physician_activated", "do_not_contact", "needs_review", "exhausted",
)

_TS = DateTime(timezone=True)


class PracticeGroup(Base):
    __tablename__ = "practice_group"
    practice_group_id = Column(Text, primary_key=True)
    practice_group_size = Column(Integer, nullable=False, default=0)
    practice_phone = Column(Text, nullable=False, default="")
    practice_address_1 = Column(Text, nullable=False, default="")
    practice_address_2 = Column(Text, nullable=False, default="")
    practice_city = Column(Text, nullable=False, default="")
    practice_state = Column(String(2), nullable=False, default="")
    practice_zip = Column(Text, nullable=False, default="")
    created_at = Column(_TS, server_default=func.now())


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (CheckConstraint("role IN ('owner','backup','system')", name="role_valid"),)
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    email = Column(Text, unique=True)
    role = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(_TS, server_default=func.now())


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (
        CheckConstraint("autonomy_level IN ('assisted','supervised','autonomous')", name="autonomy_valid"),
    )
    name = Column(Text, primary_key=True)
    label = Column(Text, nullable=False)
    specialty_terms = Column(ARRAY(Text), nullable=False, default=list)
    priority_boost = Column(Integer, nullable=False, default=0)
    pitch_angle = Column(Text, nullable=False, default="")
    autonomy_level = Column(Text, nullable=False, default="assisted")
    is_active = Column(Boolean, nullable=False, default=False)
    is_paused = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(_TS, server_default=func.now())


class Prospect(Base):
    __tablename__ = "prospect"
    npi = Column(String(10), primary_key=True)
    first_name = Column(Text, nullable=False, default="")
    middle_name = Column(Text, nullable=False, default="")
    last_name = Column(Text, nullable=False, default="")
    credential = Column(Text, nullable=False, default="")
    display_name = Column(Text, nullable=False, default="")
    primary_taxonomy_code = Column(Text, nullable=False, default="")
    primary_specialty = Column(Text, nullable=False, default="")
    practice_address_1 = Column(Text, nullable=False, default="")
    practice_address_2 = Column(Text, nullable=False, default="")
    practice_city = Column(Text, nullable=False, default="")
    practice_state = Column(String(2), nullable=False, default="")
    practice_zip = Column(Text, nullable=False, default="")
    practice_phone = Column(Text, nullable=False, default="")
    matched_zips = Column(ARRAY(Text), nullable=False, default=list)
    source = Column(Text, nullable=False, default="cms_nppes_registry_api")
    source_fetched_at = Column(_TS)
    practice_group_id = Column(Text, ForeignKey("practice_group.practice_group_id"))
    profile_url = Column(Text)
    profile_slug = Column(Text)
    created_at = Column(_TS, server_default=func.now())
    updated_at = Column(_TS, server_default=func.now())


class Contact(Base):
    __tablename__ = "contact"
    __table_args__ = (
        CheckConstraint(
            "email_status IN ('valid','risky','catch_all','unknown','invalid')", name="email_status_valid"
        ),
        UniqueConstraint("npi", "email", name="npi_email"),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    npi = Column(String(10), ForeignKey("prospect.npi"), nullable=False)
    email = Column(Text)
    email_status = Column(Text, nullable=False, default="unknown")
    verifier = Column(Text)
    verified_at = Column(_TS)
    created_at = Column(_TS, server_default=func.now())


class WorkflowScore(Base):
    __tablename__ = "workflow_score"
    __table_args__ = (
        CheckConstraint("activation_priority IN ('high','medium','low')", name="priority_valid"),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    npi = Column(String(10), ForeignKey("prospect.npi"), nullable=False)
    campaign = Column(Text, ForeignKey("campaign.name"), nullable=False, default="")
    activation_priority = Column(Text, nullable=False)
    activation_score = Column(Integer, nullable=False)
    priority_reason = Column(Text, nullable=False, default="")
    full_priority_reasons = Column(ARRAY(Text), nullable=False, default=list)
    profile_completeness_score = Column(Integer, nullable=False)
    missing_profile_fields = Column(ARRAY(Text), nullable=False, default=list)
    practice_group_id = Column(Text)
    practice_group_size = Column(Integer, nullable=False, default=0)
    model_version = Column(Text, nullable=False)
    scored_at = Column(_TS, server_default=func.now())


class Lead(Base):
    __tablename__ = "lead"
    __table_args__ = (
        CheckConstraint(
            "activation_status IN (" + ",".join(f"'{s}'" for s in LEAD_STATES) + ")",
            name="lead_status_valid",
        ),
        UniqueConstraint("npi", "campaign", name="npi_campaign"),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    npi = Column(String(10), ForeignKey("prospect.npi"), nullable=False)
    campaign = Column(Text, ForeignKey("campaign.name"), nullable=False)
    activation_status = Column(Text, nullable=False, default="not_contacted")
    cadence_step = Column(Integer, nullable=False, default=0)
    next_action_at = Column(_TS)
    stop_reason = Column(Text)
    owner = Column(Text)
    claim_url = Column(Text)
    last_polled_at = Column(_TS)
    activation_detected_at = Column(_TS)
    version = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(_TS)
    created_at = Column(_TS, server_default=func.now())
    updated_at = Column(_TS, server_default=func.now())


class Thread(Base):
    __tablename__ = "thread"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lead_id = Column(BigInteger, ForeignKey("lead.id"), nullable=False)
    reply_token = Column(Text, unique=True)
    is_locked = Column(Boolean, nullable=False, default=False)
    created_at = Column(_TS, server_default=func.now())


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (CheckConstraint("direction IN ('outbound','inbound')", name="direction_valid"),)
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lead_id = Column(BigInteger, ForeignKey("lead.id"), nullable=False)
    thread_id = Column(BigInteger, ForeignKey("thread.id"))
    npi = Column(String(10), nullable=False)
    campaign = Column(Text, ForeignKey("campaign.name"), nullable=False)
    cadence_step = Column(Integer, nullable=False)
    direction = Column(Text, nullable=False)
    variant_id = Column(Text)
    subject = Column(Text)
    body_rendered = Column(Text)
    esp_message_id = Column(Text)
    sent_at = Column(_TS)
    delivered = Column(Boolean, nullable=False, default=False)
    bounced = Column(Boolean, nullable=False, default=False)
    complained = Column(Boolean, nullable=False, default=False)
    created_at = Column(_TS, server_default=func.now())


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('delivered','opened','replied','bounced','complained',"
            "'activated','opt_out','unsubscribe_click','sent')",
            name="event_type_valid",
        ),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dedup_key = Column(Text, nullable=False)
    lead_id = Column(BigInteger, ForeignKey("lead.id"))
    message_id = Column(BigInteger, ForeignKey("message.id"))
    npi = Column(String(10))
    event_type = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    occurred_at = Column(_TS, nullable=False)
    ingested_at = Column(_TS, server_default=func.now())


class Suppression(Base):
    __tablename__ = "suppression"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('opt_out','hard_bounce','complaint','do_not_contact','manual','legal')",
            name="reason_valid",
        ),
        CheckConstraint("npi IS NOT NULL OR email IS NOT NULL", name="suppression_has_key"),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    npi = Column(String(10))
    email = Column(Text)
    reason = Column(Text, nullable=False)
    source = Column(Text, nullable=False, default="system")
    created_at = Column(_TS, server_default=func.now())


class Template(Base):
    __tablename__ = "template"
    __table_args__ = (UniqueConstraint("campaign", "version", name="campaign_version"),)
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    campaign = Column(Text, ForeignKey("campaign.name"))
    version = Column(Integer, nullable=False, default=1)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    merge_tokens = Column(ARRAY(Text), nullable=False, default=list)
    is_approved = Column(Boolean, nullable=False, default=False)
    approved_by = Column(Text)
    created_at = Column(_TS, server_default=func.now())


class Approval(Base):
    __tablename__ = "approval"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','approved','rejected','edited','expired')", name="approval_state_valid"
        ),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lead_id = Column(BigInteger, ForeignKey("lead.id"), nullable=False)
    proposed_action = Column(Text, nullable=False)
    value_tier = Column(Text)
    model_confidence = Column(Numeric(4, 3))
    gate_reason_code = Column(Text)
    proposed_subject = Column(Text)
    proposed_body = Column(Text)
    state = Column(Text, nullable=False, default="pending")
    sla_expires_at = Column(_TS)
    decided_by = Column(BigInteger, ForeignKey("app_user.id"))
    decided_at = Column(_TS)
    created_at = Column(_TS, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entity = Column(Text, nullable=False)
    entity_id = Column(Text, nullable=False)
    npi = Column(String(10))
    action = Column(Text, nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    actor = Column(Text, nullable=False)
    reason_code = Column(Text)
    created_at = Column(_TS, server_default=func.now())


class KillSwitch(Base):
    __tablename__ = "kill_switch"
    __table_args__ = (CheckConstraint("id = 1", name="kill_switch_singleton"),)
    id = Column(Integer, primary_key=True, default=1)
    is_active = Column(Boolean, nullable=False, default=False)
    set_by = Column(BigInteger, ForeignKey("app_user.id"))
    set_at = Column(_TS)


ALL_TABLES = [
    "practice_group", "app_user", "campaign", "prospect", "contact", "workflow_score",
    "lead", "thread", "message", "event", "suppression", "template", "approval",
    "audit_log", "kill_switch",
]
