"""Centralized settings (Phase 0 task C1, lightweight form).

All DSN/tokens are read from the environment HERE, never via scattered os.environ calls in
business logic. Two logical stores are kept separate (decision #1 firewall): the app/corporate
secrets (database_url, publish token) and the cold-ESP secrets (esp_api_key) — different env
prefixes so they can be sourced from different secret managers. No credential lives in the repo.

(pydantic-settings is the eventual home; kept dependency-light for now.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Settings", "get_settings"]

DEFAULT_DATABASE_URL = "postgresql+psycopg://certuma:certuma@localhost:55433/certuma"


@dataclass(frozen=True)
class Settings:
    # --- app / corporate store ---
    database_url: str = DEFAULT_DATABASE_URL
    publish_base_url: str = ""        # CERTUMALINK_API_URL (publish client target)
    publish_token: str = ""           # CERTUMALINK_API_TOKEN (admin import scope)
    # --- cold-ESP store (firewalled from corporate) ---
    esp_api_key: str = ""             # cold-outreach provider key (separate account)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            database_url=e.get("CERTUMA_DATABASE_URL", DEFAULT_DATABASE_URL),
            publish_base_url=e.get("CERTUMALINK_API_URL", ""),
            publish_token=e.get("CERTUMALINK_API_TOKEN", ""),
            esp_api_key=e.get("CERTUMA_ESP_API_KEY", ""),
        )


def get_settings() -> Settings:
    return Settings.from_env()
