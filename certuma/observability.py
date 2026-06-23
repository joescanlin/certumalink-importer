"""Structured logging + a metrics sink (Phase 0 task C3).

Because Postgres is the sole source of truth, the two failure modes that matter most - silently
clobbered live state and a silently corrupted activation metric - must surface as observable
signals, not just rows nobody reads. This module is intentionally dependency-light (stdlib JSON
logging + an in-process counter sink); the sink can be bridged to Prometheus later.

configure_logging() is NOT called at import; the app entrypoint calls it. The library/tests rely
on the METRICS counters as the assertable signal.
"""
from __future__ import annotations

import json
import logging
import sys
from threading import Lock

__all__ = ["Metrics", "METRICS", "JsonFormatter", "configure_logging", "get_logger", "emit"]


class Metrics:
    """A tiny thread-safe in-process counter sink (name + sorted label tuple -> count)."""

    def __init__(self):
        self._counters: dict = {}
        self._lock = Lock()

    @staticmethod
    def _key(name: str, labels: dict) -> tuple:
        return (name, tuple(sorted(labels.items())))

    def incr(self, name: str, value: int = 1, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def get(self, name: str, **labels) -> int:
        return self._counters.get(self._key(name, labels), 0)

    def total(self, name: str) -> int:
        return sum(v for (n, _), v in self._counters.items() if n == name)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()

    def snapshot(self) -> dict:
        return dict(self._counters)


METRICS = Metrics()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "event": record.getMessage()}
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO, stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("certuma")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str = "certuma") -> logging.Logger:
    return logging.getLogger(name)


def emit(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields) -> None:
    """Emit one structured event line. `event` is the message; `fields` are structured context."""
    logger.log(level, event, extra={"fields": fields})
