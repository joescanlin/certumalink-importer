"""certuma_core — pure, dependency-free domain logic for Certuma Reach.

Lifted from portable/certumalink-doctor-import.py with behavior parity pinned by
golden tests (tests/golden). No CSV/print/network/argparse coupling lives here.
"""
from __future__ import annotations

__all__ = [
    "models",
    "config",
    "util",
    "status",
    "specialty",
    "campaigns",
    "grouping",
    "urls",
    "scoring",
    "queue",
]
