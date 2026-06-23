"""Deterministic render step (Phase 1 task P1.8).

The model never produces the three compliance values - it leaves the tokens literal, and this step
injects {claim_url}, {unsubscribe_url}, {postal_address} deterministically. Produces the
RenderedEmail the SENDER consumes. The linter validates the rendered output afterward.
"""
from __future__ import annotations

from certuma.sender import RenderedEmail

from .provider import CopyOutput

__all__ = ["render"]


def render(
    copy: CopyOutput,
    *,
    claim_url: str,
    unsubscribe_url: str,
    unsubscribe_mailto: str,
    postal_address: str,
) -> RenderedEmail:
    def inject(text: str) -> str:
        return (
            text.replace("{claim_url}", claim_url)
            .replace("{unsubscribe_url}", unsubscribe_url)
            .replace("{postal_address}", postal_address)
        )

    return RenderedEmail(
        subject=inject(copy.subject),
        body=inject(copy.body),
        plaintext=inject(copy.plaintext),
        variant_id=copy.variant_id,
        unsubscribe_url=unsubscribe_url,
        unsubscribe_mailto=unsubscribe_mailto,
    )
