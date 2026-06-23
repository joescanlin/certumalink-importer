"""Observability + config tests (Phase 0 tasks C3 / C1, plan §8-H). Pure: no DB."""
from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma.config import Settings  # noqa: E402
from certuma.observability import METRICS, Metrics, configure_logging, emit, get_logger  # noqa: E402


class MetricsTests(unittest.TestCase):
    def test_incr_get_total_reset(self):
        m = Metrics()
        m.incr("gate_decision", decision="ALLOW")
        m.incr("gate_decision", decision="ALLOW")
        m.incr("gate_decision", decision="BLOCK", reason="suppression")
        self.assertEqual(m.get("gate_decision", decision="ALLOW"), 2)
        self.assertEqual(m.get("gate_decision", decision="BLOCK", reason="suppression"), 1)
        self.assertEqual(m.total("gate_decision"), 3)
        self.assertEqual(m.get("missing"), 0)
        m.reset()
        self.assertEqual(m.total("gate_decision"), 0)

    def test_default_sink_exists(self):
        self.assertIsInstance(METRICS, Metrics)


class StructuredLoggingTests(unittest.TestCase):
    def setUp(self):
        self._logger = logging.getLogger("certuma")
        self._saved = (list(self._logger.handlers), self._logger.level, self._logger.propagate)

    def tearDown(self):
        self._logger.handlers[:], self._logger.level, self._logger.propagate = self._saved

    def test_emit_writes_one_json_line_with_fields(self):
        stream = io.StringIO()
        configure_logging(level=logging.INFO, stream=stream)
        emit(get_logger("certuma.test"), "thing_happened", foo="bar", n=3)
        line = stream.getvalue().strip().splitlines()[-1]
        data = json.loads(line)
        self.assertEqual(data["event"], "thing_happened")
        self.assertEqual(data["foo"], "bar")
        self.assertEqual(data["n"], 3)
        self.assertEqual(data["level"], "INFO")


class SettingsTests(unittest.TestCase):
    def test_reads_injected_env_not_os_environ(self):
        s = Settings.from_env({
            "CERTUMA_DATABASE_URL": "postgresql+psycopg://u:p@h:5/db",
            "CERTUMALINK_API_URL": "https://x",
            "CERTUMALINK_API_TOKEN": "tok",
            "CERTUMA_ESP_API_KEY": "esp-secret",
        })
        self.assertTrue(s.database_url.endswith("/db"))
        self.assertEqual(s.publish_base_url, "https://x")
        self.assertEqual(s.publish_token, "tok")
        self.assertEqual(s.esp_api_key, "esp-secret")

    def test_defaults_when_absent(self):
        s = Settings.from_env({})
        self.assertEqual(s.database_url, Settings().database_url)
        self.assertEqual(s.publish_token, "")
        self.assertEqual(s.esp_api_key, "")  # cold-ESP secret kept separate, empty by default


if __name__ == "__main__":
    unittest.main(verbosity=2)
