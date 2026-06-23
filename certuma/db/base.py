"""Declarative base + naming convention.

A deterministic naming convention keeps index/constraint names stable so future
Alembic autogenerate diffs are clean. The authoritative schema is the hand-authored
initial migration (versions/0001_initial_schema.py), which matches docs/certuma-architecture
§3 exactly; these ORM models map onto those tables for the app layer (ledger_writer, repos).
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
