"""Certumalink profile-seeding publish client (Phase 0 task B13).

The endpoint contract lives in docs/profile-seeding-api.md. The endpoint is not built yet;
this client is the integration template. Credentials are injected (never read from os.environ),
and the monolith's _update_self remote-exec is intentionally NOT carried over.
"""
from .payload import PROFILE_FIELDS, build_payload, profile_payload_row
from .client import PublishError, claim_urls_by_npi, publish, publish_summary
from .claim_status import ClaimStatusUnavailable, poll_claim_urls

__all__ = [
    "PROFILE_FIELDS", "build_payload", "profile_payload_row",
    "PublishError", "publish", "publish_summary", "claim_urls_by_npi",
    "ClaimStatusUnavailable", "poll_claim_urls",
]
