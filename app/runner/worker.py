"""Background worker. For now: apply schema, then idle-poll until SIGTERM/SIGINT."""

from __future__ import annotations

import signal
import threading

from app import db
from app.config import Settings, get_settings
from app.log import bind, get_logger, setup_logging

log = get_logger(__name__)


def tick() -> int:
    """One poll iteration. Returns the number of steps processed (none yet)."""
    return 0


def run(settings: Settings, stop: threading.Event, max_ticks: int | None = None) -> int:
    """Poll until `stop` is set (or `max_ticks` reached). Returns ticks executed."""
    ticks = 0
    while not stop.is_set() and (max_ticks is None or ticks < max_ticks):
        processed = tick()
        ticks += 1
        if processed == 0:
            stop.wait(settings.runner.poll_interval_s)
    return ticks


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log.level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with bind(step="worker"):
        db.init_db(settings)
        log.info("worker started", extra={"poll_interval_s": settings.runner.poll_interval_s})
        run(settings, stop)
        log.info("worker stopping")
    db.close_pool()


if __name__ == "__main__":
    main()
