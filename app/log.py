"""JSON-lines logging to stdout with job_id / account_id / step bound via contextvars."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

BOUND_FIELDS = ("job_id", "account_id", "step")
_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

# Attributes every LogRecord has; anything else passed via `extra=` is emitted as a field.
_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Bind fields (e.g. job_id, account_id, step) to every log line inside the block."""
    token = _context.set({**current_context(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = current_context()
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "severity": record.levelname,  # Cloud Logging reads severity + message
            "logger": record.name,
            "msg": record.getMessage(),
            "message": record.getMessage(),
        }
        for f in BOUND_FIELDS:
            out[f] = ctx.get(f)
        out.update({k: v for k, v in ctx.items() if k not in BOUND_FIELDS})
        out.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def setup_logging(level: str) -> None:
    """Route the root logger to stdout as JSON lines. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
