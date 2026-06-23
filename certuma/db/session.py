"""Engine + session factory, built from Settings (never reads env directly)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from certuma.config import Settings, get_settings

__all__ = ["make_engine", "make_session_factory", "get_engine"]


def make_engine(settings: Settings | None = None, **kwargs) -> Engine:
    settings = settings or get_settings()
    return create_engine(settings.database_url, future=True, **kwargs)


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False, future=True)


_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide lazy engine for app use."""
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine
