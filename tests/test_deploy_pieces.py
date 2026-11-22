"""Deploy pieces that run without GCP: Cloud SQL conninfo, log fields, START_MODE override,
worker --until-idle."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from app import db
from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from app.log import JsonFormatter
from app.runner import worker as worker_mod
from app.runner.registry import Registry

S = load_settings(DEFAULT_CONFIG_PATH)
TOKEN = "t0ken"


def with_secrets(**kw: Any) -> Settings:
    return S.model_copy(update={"secrets": S.secrets.model_copy(update=kw)})


# --- Cloud SQL conninfo and logs ---------------------------------------------------------


def test_database_url_from_cloud_sql_parts() -> None:
    s = with_secrets(
        database_url=None,
        db_user="revagent",
        db_password=SecretStr("p w"),
        db_name="revagent",
        db_host="/cloudsql/p:asia-south1:revagent-pg",
    )
    info = psycopg.conninfo.conninfo_to_dict(db.database_url(s))
    assert info == {
        "user": "revagent",
        "password": "p w",
        "dbname": "revagent",
        "host": "/cloudsql/p:asia-south1:revagent-pg",
    }
    with pytest.raises(RuntimeError, match="DB_HOST"):
        db.database_url(with_secrets(database_url=None, db_user=None))


def test_log_lines_carry_severity_and_message() -> None:
    rec = logging.makeLogRecord({"msg": "step end", "levelname": "WARNING", "name": "x"})
    out = json.loads(JsonFormatter().format(rec))
    assert out["severity"] == "WARNING" and out["message"] == "step end"
    assert out["level"] == "WARNING" and out["msg"] == "step end"  # old fields kept


# --- worker --until-idle -----------------------------------------------------------------


def test_worker_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_JOB_ID", "abc")
    a = worker_mod._args(["--until-idle"])
    assert a.until_idle is True and a.job_id == "abc"


def test_until_idle_exits_when_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, db_url: str, pg_conn: psycopg.Connection
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    s = load_settings(DEFAULT_CONFIG_PATH)
    db.close_pool()
    db.init_db(s)
    w = worker_mod.Worker(s, Registry(), job_id=uuid.uuid4())  # a job with no steps
    try:
        assert w.run(until_idle=True) == 0
    finally:
        w.close()
        db.close_pool()


async def test_start_mode_env_override_triggers_cloud_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import start

    fired: list[Any] = []
    monkeypatch.setattr(start, "insert_job", lambda *a: "job-1")

    async def fake_trigger(settings: Settings, job_id: Any) -> int:
        fired.append(job_id)
        return 200

    monkeypatch.setattr(start, "trigger_cloud_run", fake_trigger)
    assert S.api.start_mode == "local"
    await start.start(S, Registry(), {"request": "x"}, {})
    assert fired == []  # config says local
    await start.start(with_secrets(start_mode="cloud_run"), Registry(), {"request": "x"}, {})
    assert fired == ["job-1"]  # START_MODE=cloud_run on Cloud Run
