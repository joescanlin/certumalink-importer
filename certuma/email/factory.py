"""Provider factory (Phase 1 task P1.2). Selects the EmailProvider from Settings."""
from __future__ import annotations

from certuma.config import Settings, get_settings

from .esp import EspProvider
from .mailpit import MailpitProvider
from .provider import EmailProvider

__all__ = ["get_provider"]


def get_provider(settings: Settings = None) -> EmailProvider:
    settings = settings or get_settings()
    if settings.email_provider == "mailpit":
        return MailpitProvider(settings.smtp_host, settings.smtp_port)
    if settings.email_provider == "esp":
        return EspProvider(settings)
    raise ValueError(f"unknown CERTUMA_EMAIL_PROVIDER: {settings.email_provider!r}")
