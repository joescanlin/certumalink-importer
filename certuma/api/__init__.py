"""Certuma sales dashboard backend (Phase 0 tasks B14/B15) - thin read-mostly FastAPI app."""
from .app import create_app, get_db

__all__ = ["create_app", "get_db"]
