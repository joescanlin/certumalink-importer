"""Quiet-hours tests (Phase 1 task P1.3). Pure."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma_core.quiet_hours import is_quiet_hours  # noqa: E402


def _utc(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


class QuietHoursTests(unittest.TestCase):
    def test_business_hours_not_quiet(self):
        # 2026-06-23 is a Tuesday; 14:00 UTC = 09:00 CDT in TX -> business
        self.assertFalse(is_quiet_hours("TX", _utc(2026, 6, 23, 14)))

    def test_after_hours_quiet(self):
        # 02:00 UTC Tue = 21:00 Mon CDT -> quiet
        self.assertTrue(is_quiet_hours("TX", _utc(2026, 6, 23, 2)))

    def test_weekend_quiet(self):
        # 2026-06-20 is a Saturday
        self.assertTrue(is_quiet_hours("CA", _utc(2026, 6, 20, 18)))

    def test_unknown_and_blank_state_fail_safe(self):
        self.assertTrue(is_quiet_hours("ZZ", _utc(2026, 6, 23, 14)))
        self.assertTrue(is_quiet_hours("", _utc(2026, 6, 23, 14)))

    def test_multi_tz_state_uses_widest_quiet_window(self):
        # 13:00 UTC Tue = 08:00 CDT (business in Chicago) but 07:00 MDT (quiet in Denver).
        # TX spans both -> quiet (widest window).
        self.assertTrue(is_quiet_hours("TX", _utc(2026, 6, 23, 13)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
