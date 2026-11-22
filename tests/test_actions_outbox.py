"""Outbox state machine against Postgres (skipped when the compose DB isn't running):
concurrent execute_ready -> one event; crash after the side effect -> restart marks done
without a second send; approval and payload checks; audit rows."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app import db
from app.actions import outbox
from app.actions.effects import Effects, Sink
from app.actions.models import State, make_key
from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings
from tests.actions_helpers import FakeGoogle
from tests.test_actions_effects import CAL, MAIL


@pytest.fixture
def s(
    monkeypatch: pytest.MonkeyPatch, db_url: str, pg_conn: psycopg.Connection, tmp_path: Path
) -> Iterator[Settings]:
    monkeypatch.setenv("DATABASE_URL", db_url)
    base = load_settings(DEFAULT_CONFIG_PATH)
    a = base.actions.model_copy(
        update={"sink_path": str(tmp_path / "sink.jsonl"), "calendar_mode": "google"}
    )
    settings = base.model_copy(update={"actions": a})
    db.close_pool()
    db.init_db(settings)
    yield settings
    db.close_pool()


def new_action(kind: str, payload: dict, approve: bool = True) -> str:
    with db.tx() as conn:
        job = conn.execute(
            "INSERT INTO jobs (client_id, spec, status, limits) "
            "VALUES ('test', '{}', 'running', '{}') RETURNING id"
        ).fetchone()["id"]
        key = make_key(kind, job, uuid.uuid4())
        outbox.propose(conn, key=key, job_id=job, account_id=None, kind=kind, payload=payload)
        if approve:
            outbox.approve(conn, key, "reviewer@3pl.example")
    return key


def state(key: str) -> dict:
    with db.tx() as conn:
        return conn.execute("SELECT * FROM actions WHERE idem_key = %s", (key,)).fetchone()


def audit_reasons(key: str) -> list[str]:
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT reason FROM audit_log WHERE tbl = 'actions' AND record_id = %s ORDER BY id",
            (key,),
        ).fetchall()
    return [r["reason"] for r in rows]


def expire_lease(key: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE actions SET updated_at = now() - interval '1 day' WHERE idem_key = %s", (key,)
        )


async def test_two_concurrent_execute_ready_make_one_event(s: Settings) -> None:
    key = new_action("calendar", CAL)
    g = FakeGoogle(delay_s=0.2)  # slow side effect: the runs overlap
    fx = Effects(s, call=g)
    a, b = await asyncio.gather(
        outbox.execute_ready(fx, settings=s, owner="w1"),
        outbox.execute_ready(fx, settings=s, owner="w2"),
    )
    mine = [o for o in (*a, *b) if o.status == "done"]
    assert len(mine) == 1
    assert len(g.events) == 1 and g.posts("gcal:events.insert") == 1
    assert state(key)["state"] == State.done


class CrashError(Exception):
    pass


@pytest.mark.parametrize("kind", ["calendar", "email"])
async def test_crash_after_side_effect_restart_marks_done_without_second_send(
    s: Settings, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    key = new_action(kind, CAL if kind == "calendar" else MAIL)
    g = FakeGoogle()
    fx = Effects(s, call=g)

    real_finish = outbox.finish

    def crash(*_a, **_k):
        raise CrashError("worker died after the side effect, before mark-done")

    monkeypatch.setattr(outbox, "finish", crash)
    with pytest.raises(CrashError):
        await outbox.execute_ready(fx, settings=s, owner="w1")
    assert state(key)["state"] == State.executing  # side effect happened, not marked
    # Within the lease, nobody touches it.
    monkeypatch.setattr(outbox, "finish", real_finish)
    assert await outbox.execute_ready(fx, settings=s, owner="w2") == []

    expire_lease(key)  # the dead worker's lease runs out
    [again] = await outbox.execute_ready(fx, settings=s, owner="w2")
    assert again.status == "done"
    row = state(key)
    assert row["state"] == State.done and row["attempts"] == 2
    if kind == "calendar":
        assert row["result"]["status"] == "already_existed"  # 409 on the second insert
        assert len(g.events) == 1
    else:
        assert row["result"]["status"] == "already_sent"  # found in the sink log
        assert len(Sink(Path(s.actions.sink_path)).entries()) == 1
    reasons = audit_reasons(key)
    assert reasons[:3] == ["proposed", "approved by reviewer@3pl.example", "claimed"]
    assert "reclaimed after lease expiry" in reasons and reasons[-1] == "side effect done"


async def test_nothing_runs_without_approval(s: Settings) -> None:
    key = new_action("email", MAIL, approve=False)
    assert await outbox.execute_ready(Effects(s), settings=s, owner="w") == []
    with db.tx() as conn:  # even a row forced to 'approved' needs an approvals row
        conn.execute("UPDATE actions SET state = 'approved' WHERE idem_key = %s", (key,))
    assert await outbox.execute_ready(Effects(s), settings=s, owner="w") == []


async def test_payload_changed_after_approval_fails(s: Settings) -> None:
    key = new_action("calendar", CAL)
    with db.tx() as conn:
        conn.execute(
            "UPDATE actions SET payload = jsonb_set(payload, '{attendees}', "
            "'[\"someone.else@x.in\"]') WHERE idem_key = %s",
            (key,),
        )
    g = FakeGoogle()
    [out] = await outbox.execute_ready(Effects(s, call=g), settings=s, owner="w")
    assert out.status == "failed" and "approved payload" in out.error
    assert g.calls == [] and state(key)["state"] == State.failed


def test_rejected_is_terminal(s: Settings) -> None:
    key = new_action("email", MAIL, approve=False)
    with db.tx() as conn:
        outbox.reject(conn, key, "reviewer@3pl.example", "wrong contact")
    with pytest.raises(outbox.InvalidTransition), db.tx() as conn:
        outbox.approve(conn, key, "reviewer@3pl.example")
    assert state(key)["state"] == State.rejected


def test_propose_is_idempotent(s: Settings) -> None:
    key = new_action("email", MAIL, approve=False)
    with db.tx() as conn:
        job = state(key)["job_id"]
        again = outbox.propose(
            conn,
            key=key,
            job_id=job,
            account_id=None,
            kind="email",
            payload={**MAIL, "subject": "changed"},
        )
    assert again is False and state(key)["payload"]["subject"] == MAIL["subject"]


async def test_retry_stays_executing_then_gives_up(s: Settings) -> None:
    key = new_action("calendar", CAL)
    g = FakeGoogle()
    g.fail["gcal:events.insert"] = 503
    fx = Effects(s, call=g)
    for _ in range(s.actions.max_attempts):
        await outbox.execute_ready(fx, settings=s, owner="w")
        assert state(key)["state"] == State.executing
        expire_lease(key)
    await outbox.execute_ready(fx, settings=s, owner="w")
    row = state(key)
    assert row["state"] == State.failed and "gave up" in row["last_error"]
