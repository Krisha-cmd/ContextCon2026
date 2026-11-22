"""Heartbeat thread: extends a step's lease while it runs."""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.log import get_logger

log = get_logger(__name__)


class Heartbeat:
    """Calls `beat()` every `interval_s` until stopped. `beat` returns False if the lease is
    gone; then `lost` is set (the step's commit will be refused, so it can't double-write)."""

    def __init__(self, beat: Callable[[], bool], interval_s: float) -> None:
        self._beat = beat
        self._interval = interval_s
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.beats = 0
        self._thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                ok = self._beat()
            except Exception:  # keep beating; a DB blip shouldn't kill the thread
                log.exception("heartbeat failed")
                continue
            self.beats += 1
            if not ok:
                log.warning("lease lost")
                self.lost.set()
                return
