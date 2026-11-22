from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import main


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Unit test: no real DB. Lifespan is skipped by not using the client as a context manager.
    monkeypatch.setattr(db, "init_db", lambda *_a, **_k: None)
    return TestClient(main.app)


def test_health_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", lambda: True)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_health_db_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> bool:
        raise RuntimeError("no db")

    monkeypatch.setattr(db, "ping", boom)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["db"] == "down"
