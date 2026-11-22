from __future__ import annotations

import threading

from app.config import DEFAULT_CONFIG_PATH, load_settings
from app.runner import worker


def test_run_stops_when_event_set() -> None:
    stop = threading.Event()
    stop.set()
    assert worker.run(load_settings(DEFAULT_CONFIG_PATH), stop) == 0


def test_run_respects_max_ticks(monkeypatch) -> None:
    settings = load_settings(DEFAULT_CONFIG_PATH)
    calls: list[float] = []

    class FakeEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            calls.append(timeout or 0)
            return False

    assert worker.run(settings, FakeEvent(), max_ticks=3) == 3
    assert calls == [settings.runner.poll_interval_s] * 3
