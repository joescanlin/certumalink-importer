"""Centralized settings (Phase 0 task C1, extended for Phase 1 task P1.0).

All DSN/tokens/config are read from the environment HERE, never via scattered os.environ calls in
business logic. Two logical stores are kept separate (decision #1 firewall): the app/corporate
secrets (database_url, publish token) and the cold-ESP / sending secrets (esp_api_key, provider,
sender identity, enrichment keys) - different env prefixes so they can be sourced from different
secret managers. No credential lives in the repo.

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

    # --- cold-ESP / sending store (firewalled from corporate) ---
    esp_api_key: str = ""             # cold-outreach provider key (separate account)
    esp_base_url: str = ""            # cold-tolerant ESP API base (CERTUMA_ESP_BASE_URL)
    webhook_secret: str = ""          # shared secret for provider event/inbound webhooks
    email_provider: str = "mailpit"   # 'mailpit' (dev) | 'esp' (cold-tolerant provider, later)
    smtp_host: str = "127.0.0.1"      # Mailpit dev SMTP (certumalocal)
    smtp_port: int = 11025
    cold_domain: str = ""             # e.g. getcertuma.com (deferred infra); placeholder until set
    reply_to_domain: str = ""         # plus-addressed Reply-To domain (defaults to cold_domain)
    sender_from_name: str = ""        # the real accountable employee (decision #5)
    sender_from_title: str = ""
    sender_from_email: str = ""
    postal_address: str = ""          # CAN-SPAM physical postal address for the footer

    # --- enrichment store ---
    enrich_api_key: str = ""          # discovery provider (healthcare-specialized / B2B)
    verify_api_key: str = ""          # email-verification provider

    # --- dashboard auth (P3.9) ---
    session_secret: str = ""          # CERTUMA_SESSION_SECRET; signs the session cookie

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def _int(key: str, default: int) -> int:
            raw = e.get(key)
            try:
                return int(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        return cls(
            database_url=e.get("CERTUMA_DATABASE_URL", DEFAULT_DATABASE_URL),
            publish_base_url=e.get("CERTUMALINK_API_URL", ""),
            publish_token=e.get("CERTUMALINK_API_TOKEN", ""),
            esp_api_key=e.get("CERTUMA_ESP_API_KEY", ""),
            esp_base_url=e.get("CERTUMA_ESP_BASE_URL", ""),
            webhook_secret=e.get("CERTUMA_WEBHOOK_SECRET", ""),
            email_provider=e.get("CERTUMA_EMAIL_PROVIDER", "mailpit"),
            smtp_host=e.get("CERTUMA_SMTP_HOST", "127.0.0.1"),
            smtp_port=_int("CERTUMA_SMTP_PORT", 11025),
            cold_domain=e.get("CERTUMA_COLD_DOMAIN", ""),
            reply_to_domain=e.get("CERTUMA_REPLY_TO_DOMAIN", "") or e.get("CERTUMA_COLD_DOMAIN", ""),
            sender_from_name=e.get("CERTUMA_SENDER_FROM_NAME", ""),
            sender_from_title=e.get("CERTUMA_SENDER_FROM_TITLE", ""),
            sender_from_email=e.get("CERTUMA_SENDER_FROM_EMAIL", ""),
            postal_address=e.get("CERTUMA_POSTAL_ADDRESS", ""),
            enrich_api_key=e.get("CERTUMA_ENRICH_API_KEY", ""),
            verify_api_key=e.get("CERTUMA_VERIFY_API_KEY", ""),
            session_secret=e.get("CERTUMA_SESSION_SECRET", ""),
        )


def get_settings() -> Settings:
    return Settings.from_env()
