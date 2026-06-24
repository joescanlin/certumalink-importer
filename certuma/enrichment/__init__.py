"""Enrichment waterfall (Phase 2 task P2.5) - the real front of the loop.

Turns a raw prospect into a sendable lead by finding and verifying a deliverable email, replacing
the demo's hand-seeded contact. A discovery provider proposes candidate addresses; a verification
provider scores each; the waterfall keeps the best VALID one (a real mailbox is preferred over a
role address like info@), records it as a Contact, and advances the lead to sendable - or routes it
to needs_review when nothing deliverable exists. Suppressed prospects are skipped before any spend.

Deterministic stubs back the tests and the dev loop; real discovery/verification vendors slot in
behind the same interfaces at the infra cutover (decision 3: stub-and-seam now).
"""
from .provider import EmailCandidate, EnrichProvider, VerifyProvider, VerifyResult
from .stub import StubEnrichProvider, StubVerifyProvider
from .node import EnrichOutcome, EnrichSummary, enrich_lead, run_enrichment

__all__ = [
    "EmailCandidate", "VerifyResult", "EnrichProvider", "VerifyProvider",
    "StubEnrichProvider", "StubVerifyProvider",
    "EnrichOutcome", "EnrichSummary", "enrich_lead", "run_enrichment",
]
