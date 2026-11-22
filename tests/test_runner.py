"""app/runner against the compose Postgres (port 5433). Each test uses its own Registry and
its own job, and workers are restricted to that job, so tests never touch each other."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from app import db
from app.config import DEFAULT_CONFIG_PATH, ROOT, Settings, load_settings, read_yaml
from app.gateway import BudgetExceeded
from app.runner.context import StepContext
from app.runner.engine import Engine
from app.runner.jobs import create_job, job_status, resume_job
from app.runner.registry import Registry
from app.runner.worker import Worker

FAST_RUNNER = {
    "poll_interval_s": 0.05,
    "lease_s": 2,
    "heartbeat_s": 0.3,
    "retry_backoff_base_s": 0.05,
    "retry_backoff_max_s": 0.1,
}


def fast_config(**runner: Any) -> dict[str, Any]:
    data = read_yaml(DEFAULT_CONFIG_PATH)
    data["runner"] = {**data["runner"], **FAST_RUNNER, **runner}
    return data


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, db_url: str, pg_conn: psycopg.Connection,
             tmp_path: Path) -> Iterator[Settings]:  # fmt: skip
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(fast_config()), encoding="utf-8")
    s = load_settings(cfg)
    db.apply_schema(pg_conn, s.db.schema_lock_key)
    db.close_pool()
    db.open_pool(s)
    yield s
    db.close_pool()


def with_runner(s: Settings, **changes: Any) -> Settings:
    return s.model_copy(update={"runner": s.runner.model_copy(update=changes)})


def new_job(s: Settings, reg: Registry) -> Any:
    with db.tx() as conn:
        return create_job(conn, s, reg, {"request": "test"}, "test-client")


def run_job(s: Settings, reg: Registry, job: Any, **kw: Any) -> Worker:
    w = Worker(s, reg, job_id=job, **kw)
    try:
        w.run(until_job_done=True)
    finally:
        w.close()
    return w


def q(sql: str, *params: Any) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return conn.execute(sql, params).fetchall()


def steps_of(job: Any) -> list[dict[str, Any]]:
    return q(
        "SELECT s.step, a.name AS account, s.status, s.attempts, s.output, s.lease_owner "
        "FROM steps s LEFT JOIN accounts a ON a.id = s.account_id WHERE s.job_id = %s "
        "ORDER BY s.priority, a.name",
        job,
    )


def job_row(job: Any) -> dict[str, Any]:
    return q("SELECT * FROM jobs WHERE id = %s", job)[0]


def accounts_of(job: Any) -> dict[str, dict[str, Any]]:
    rows = q("SELECT * FROM accounts WHERE job_id = %s", job)
    return {r["name"]: r for r in rows}


def audit_reasons(job: Any) -> list[str]:
    return [
        r["reason"] for r in q("SELECT reason FROM audit_log WHERE job_id = %s ORDER BY id", job)
    ]


def discover(*names: str):
    def s1(ctx: StepContext) -> dict:
        for n in names:
            ctx.add_account(n, f"{n.lower()}.example")
        return {"found": len(names)}

    return s1


# ---- registry ---------------------------------------------------------------------------
def test_registry_order_and_validation() -> None:
    reg = Registry()
    reg.step("b", per_account=True, order=20)(lambda c: None)
    reg.step("a", per_account=False, order=10)(lambda c: None)
    reg.step("c", per_account=False, order=30)(lambda c: None)
    assert reg.names() == ["a", "b", "c"]
    assert reg.first().name == "a"
    assert reg.after("a").name == "b" and reg.after("c") is None
    assert reg.first_job_level_after(10).name == "c"
    with pytest.raises(ValueError):
        reg.step("a", per_account=False, order=99)(lambda c: None)
    with pytest.raises(ValueError):
        reg.step("d", per_account=False, order=10)(lambda c: None)


def test_default_pipeline_order() -> None:
    import app.runner.pipeline  # noqa: F401
    from app.runner.registry import REGISTRY

    assert REGISTRY.names() == ["parse", "discover", "dedup", "pre_rank", "identity", "registry",
                                "news", "enrich", "trigger", "buyer", "score", "draft"]  # fmt: skip


# ---- kill and restart -------------------------------------------------------------------
def _harness(job: Any, run_log: Path, cfg: Path, db_url: str, kill_marker: Path | None) -> int:
    env = {**os.environ, "CONFIG_PATH": str(cfg), "DATABASE_URL": db_url, "LOG_LEVEL": "WARNING"}
    env.pop("KILL_MARKER", None)
    if kill_marker is not None:
        env["KILL_MARKER"] = str(kill_marker)
    proc = subprocess.run(
        [sys.executable, "-m", "tests.runner_harness", str(job), str(run_log)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    return proc.returncode


def test_hard_kill_mid_step_then_restart(settings: Settings, db_url: str, tmp_path: Path) -> None:
    from tests.runner_harness import HARD_KILL_EXIT, REG

    cfg = tmp_path / "config.yaml"  # written by the settings fixture (lease_s = 2)
    run_log, marker = tmp_path / "runs.log", tmp_path / "killed"
    job = new_job(settings, REG)

    # Run 1: s1 completes, then the process dies inside s2 for the first account.
    assert _harness(job, run_log, cfg, db_url, marker) == HARD_KILL_EXIT
    killed = marker.read_text(encoding="utf-8")
    after_kill = {(s["step"], s["account"]): s for s in steps_of(job)}
    assert after_kill[("s1", None)]["status"] == "done"
    assert after_kill[("s2", killed)]["status"] == "running"  # dead lease, still held
    assert after_kill[("s2", killed)]["lease_owner"] is not None

    # Run 2: waits for the dead lease to expire, reclaims it, finishes the job.
    assert _harness(job, run_log, cfg, db_url, marker) == 0
    runs = run_log.read_text(encoding="utf-8").split()
    other = "B" if killed == "A" else "A"
    assert runs.count("s1") == 1  # not re-run
    assert runs.count(f"s2:{killed}") == 2  # killed once, then completed
    assert runs.count(f"s2:{other}") == 1
    assert runs.count("s3") == 1

    final = {(s["step"], s["account"]): s for s in steps_of(job)}
    assert {s["status"] for s in final.values()} == {"done"}
    assert final[("s2", killed)]["attempts"] == 2
    assert final[("s2", killed)]["output"] == {"account": killed}
    assert job_row(job)["status"] == "done"

    rows = q("SELECT step, tbl, record_id, reason FROM audit_log WHERE job_id = %s", job)
    keys = [(r["step"], r["tbl"], r["record_id"], r["reason"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate audit rows"
    reasons = [k[3] for k in keys]
    assert reasons.count("step completed") == 4  # s1, s2 x2, s3
    assert reasons.count("dummy audit from s1") == 1
    assert reasons.count("dummy audit from s2") == 2
    assert reasons.count("account discovered") == 2


def test_dead_lease_is_reclaimed(settings: Settings) -> None:
    reg = Registry()
    reg.step("only", per_account=False, order=1)(lambda ctx: {"ok": True})
    job = new_job(settings, reg)
    with db.tx() as conn:  # a worker claims, then dies without a trace
        claim = Engine(conn, settings, reg).claim_one("ghost-worker", job)
    assert claim.step["lease_owner"] == "ghost-worker"
    q("UPDATE steps SET lease_until = now() - interval '1 second' WHERE id = %s RETURNING id",
      claim.step["id"])  # fmt: skip

    run_job(settings, reg, job, owner="rescuer")
    (row,) = steps_of(job)
    assert row["status"] == "done" and row["attempts"] == 2 and row["lease_owner"] is None
    assert job_row(job)["status"] == "done"


def test_live_lease_is_not_stolen(settings: Settings) -> None:
    reg = Registry()
    reg.step("only", per_account=False, order=1)(lambda ctx: {"ok": True})
    job = new_job(settings, reg)
    with db.tx() as conn:
        assert Engine(conn, settings, reg).claim_one("owner-1", job)
    with db.tx() as conn:
        assert Engine(conn, settings, reg).claim_one("owner-2", job) is None


# ---- accounts: drop / hold --------------------------------------------------------------
def test_dropped_and_held_accounts_skip_later_steps(settings: Settings) -> None:
    reg, seen = Registry(), []
    reg.step("s1", per_account=False, order=1)(discover("A", "B", "C"))

    @reg.step("s2", per_account=True, order=2)
    def check(ctx: StepContext) -> dict:
        if ctx.account["name"] == "B":
            ctx.drop("not a 3PL buyer", evidence_ids=("ev_1",))
        if ctx.account["name"] == "C":
            ctx.hold("registry status unclear")
        return {"checked": ctx.account["name"]}

    @reg.step("s3", per_account=True, order=3)
    def after(ctx: StepContext) -> dict:
        seen.append(ctx.account["name"])
        return {}

    job = new_job(settings, reg)
    run_job(settings, reg, job)
    assert seen == ["A"]
    acc = accounts_of(job)
    assert (acc["B"]["status"], acc["B"]["status_reason"], acc["B"]["status_step"]) == (
        "dropped", "not a 3PL buyer", "s2")  # fmt: skip
    assert acc["C"]["status"] == "held"
    drop_audit = q("SELECT * FROM audit_log WHERE job_id = %s AND reason = 'not a 3PL buyer'", job)
    assert drop_audit[0]["evidence_ids"] == ["ev_1"]
    assert drop_audit[0]["after"]["status"] == "dropped"
    assert job_row(job)["status"] == "done"


def test_no_accounts_still_reaches_next_job_level_step(settings: Settings) -> None:
    reg, ran = Registry(), []
    reg.step("s1", per_account=False, order=1)(discover())
    reg.step("s2", per_account=True, order=2)(lambda ctx: ran.append("s2") or {})
    reg.step("s3", per_account=False, order=3)(lambda ctx: ran.append("s3") or {})
    job = new_job(settings, reg)
    run_job(settings, reg, job)
    assert ran == ["s3"]
    assert job_row(job)["status"] == "done"


# ---- failures ---------------------------------------------------------------------------
def test_failures_retry_then_dead_letter_and_hold(settings: Settings) -> None:
    reg, calls = Registry(), []
    reg.step("s1", per_account=False, order=1)(discover("A", "B"))

    @reg.step("s2", per_account=True, order=2)
    def flaky(ctx: StepContext) -> dict:
        calls.append(ctx.account["name"])
        if ctx.account["name"] == "B":
            raise RuntimeError("registry API down")
        return {}

    reg.step("s3", per_account=False, order=3)(lambda ctx: {"final": True})
    job = new_job(settings, reg)
    run_job(settings, reg, job)

    assert calls.count("B") == settings.runner.max_attempts
    final = {(s["step"], s["account"]): s for s in steps_of(job)}
    assert final[("s2", "B")]["status"] == "failed"
    assert final[("s3", None)]["status"] == "done"  # the job carries on without B
    dl = q(
        "SELECT d.* FROM dead_letter d JOIN steps s ON s.id = d.step_id WHERE s.job_id = %s", job
    )
    assert len(dl) == 1 and dl[0]["attempts"] == settings.runner.max_attempts
    assert "RuntimeError: registry API down" in dl[0]["error"]
    b = accounts_of(job)["B"]
    assert b["status"] == "held"
    assert b["status_reason"].startswith("step failed: s2")
    reasons = audit_reasons(job)
    assert sum(r.startswith("attempt ") for r in reasons) == settings.runner.max_attempts - 1
    assert job_row(job)["status"] == "done"


def test_core_job_level_failure_fails_job_and_resume_requeues(settings: Settings) -> None:
    reg, state = Registry(), {"broken": True}

    @reg.step("s1", per_account=False, order=1)
    def core(ctx: StepContext) -> dict:
        if state["broken"]:
            raise ValueError("bad spec")
        return {"ok": True}

    job = new_job(settings, reg)
    run_job(settings, reg, job)
    assert job_row(job)["status"] == "failed"

    state["broken"] = False
    with db.tx() as conn:
        resume_job(conn, settings, reg, job)
    run_job(settings, reg, job)
    assert job_row(job)["status"] == "done"
    assert "requeued on resume" in audit_reasons(job)


def test_non_json_output_is_a_failure(settings: Settings) -> None:
    reg = Registry()
    reg.step("s1", per_account=False, order=1)(lambda ctx: {"bad": object()})
    s = with_runner(settings, max_attempts=1)
    job = new_job(s, reg)
    run_job(s, reg, job)
    (row,) = steps_of(job)
    assert row["status"] == "failed"


# ---- budget and time limits -------------------------------------------------------------
def _limits_registry(spend: list[str]) -> Registry:
    reg = Registry()
    reg.step("s1", per_account=False, order=1)(discover("A", "B"))

    @reg.step("opt_a", per_account=True, order=2)
    def opt_a(ctx: StepContext) -> dict:
        spend.append(f"opt_a:{ctx.account['name']}")
        raise BudgetExceeded(ctx.job_id, 2500.0, 60.0, 2500.0)

    reg.step("opt_b", per_account=True, order=3)(lambda ctx: spend.append("opt_b") or {})
    reg.step("core", per_account=False, order=4)(lambda ctx: spend.append("core") or {"ok": 1})
    return reg


def test_budget_exceeded_skips_optional_and_finishes_core(settings: Settings) -> None:
    spend: list[str] = []
    s = with_runner(settings, optional_steps=("opt_a", "opt_b"))
    reg = _limits_registry(spend)
    job = new_job(s, reg)
    run_job(s, reg, job)

    assert spend.count("opt_b") == 0
    assert sum(x.startswith("opt_a") for x in spend) == 1  # the other account's opt_a skipped
    assert spend[-1] == "core"
    final = steps_of(job)
    assert {r["status"] for r in final if r["step"] in ("opt_a", "opt_b")} == {"skipped"}
    assert job_row(job)["stop_optional"] == "budget_exceeded"
    assert job_row(job)["status"] == "done"
    assert any(r.startswith("optional steps stopped: budget_exceeded") for r in audit_reasons(job))
    assert {a["status"] for a in accounts_of(job).values()} == {"active"}  # optional: no hold


def test_budget_exceeded_in_core_per_account_step_holds_account(settings: Settings) -> None:
    spend: list[str] = []
    s = with_runner(settings, optional_steps=("opt_b",))  # opt_a is core here
    reg = _limits_registry(spend)
    job = new_job(s, reg)
    run_job(s, reg, job)
    acc = accounts_of(job)
    held = [a for a in acc.values() if a["status"] == "held"]
    assert held and all(a["status_reason"].startswith("budget exceeded during opt_a") for a in held)
    assert job_row(job)["status"] == "done"


def test_time_limit_skips_optional_work(settings: Settings) -> None:
    ran: list[str] = []
    reg = Registry()
    reg.step("s1", per_account=False, order=1)(discover("A"))
    reg.step("opt", per_account=True, order=2)(lambda ctx: ran.append("opt") or {})
    reg.step("core", per_account=True, order=3)(lambda ctx: ran.append("core") or {})
    s = with_runner(settings, optional_steps=("opt",))
    job = new_job(s, reg)
    minutes = s.limits.max_minutes_per_job + 1
    q("UPDATE jobs SET created_at = now() - make_interval(mins => %s) WHERE id = %s RETURNING id",
      minutes, job)  # fmt: skip
    run_job(s, reg, job)
    assert ran == ["core"]
    assert job_row(job)["stop_optional"] == "time_limit"
    assert job_row(job)["status"] == "done"


# ---- shutdown & heartbeat ---------------------------------------------------------------
def test_sigterm_releases_lease_cleanly(settings: Settings) -> None:
    import asyncio

    reg = Registry()

    @reg.step("slow", per_account=False, order=1)
    async def slow(ctx: StepContext) -> dict:
        await asyncio.sleep(30)
        return {}

    job = new_job(settings, reg)
    w = Worker(settings, reg, job_id=job)
    threading.Timer(0.5, w.request_stop).start()  # what the SIGTERM handler calls
    try:
        assert w.run_once() is True
    finally:
        w.close()
    (row,) = q("SELECT * FROM steps WHERE job_id = %s", job)
    assert (row["status"], row["lease_owner"], row["lease_until"], row["attempts"]) == (
        "pending", None, None, 0)  # fmt: skip
    assert "lease released on shutdown" in audit_reasons(job)


def test_heartbeat_keeps_long_step_leased(settings: Settings) -> None:
    import time

    reg, stolen = Registry(), []
    s = with_runner(settings, lease_s=1, heartbeat_s=0.2)

    @reg.step("long", per_account=False, order=1)
    def long(ctx: StepContext) -> dict:
        deadline = time.monotonic() + 2.5  # longer than the 1 s lease
        while time.monotonic() < deadline:
            with db.tx() as conn:
                stolen.append(Engine(conn, s, reg).claim_one("thief", ctx.job_id))
            time.sleep(0.3)
        return {"ok": True}

    job = new_job(s, reg)
    run_job(s, reg, job)
    assert all(x is None for x in stolen)  # never reclaimable while the heartbeat runs
    (row,) = steps_of(job)
    assert row["status"] == "done" and row["attempts"] == 1


def test_stale_worker_cannot_commit(settings: Settings) -> None:
    reg = Registry()
    reg.step("only", per_account=False, order=1)(lambda ctx: {"ok": True})
    job = new_job(settings, reg)
    with db.tx() as conn:
        old = Engine(conn, settings, reg).claim_one("old", job)
    q("UPDATE steps SET lease_owner = 'new' WHERE id = %s RETURNING id", old.step["id"])
    w = Worker(settings, reg, job_id=job, owner="old")
    try:
        w.execute(old)  # runs, but its commit must be refused
    finally:
        w.close()
    (row,) = steps_of(job)
    assert row["status"] == "running" and row["lease_owner"] == "new"
    assert "step completed" not in audit_reasons(job)


# ---- CLI ---------------------------------------------------------------------------------
def test_cli_run_and_status(settings: Settings, db_url: str, monkeypatch, capsys,
                            tmp_path: Path) -> None:  # fmt: skip
    from app.config import get_settings
    from app.runner.__main__ import main

    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    get_settings.cache_clear()
    reg = Registry()
    reg.step("only", per_account=False, order=1)(lambda ctx: {"spec": ctx.job["spec"]})
    try:
        assert main(["run", "--request", "3PL leads in Pune"], registry=reg) == 0
        out = capsys.readouterr()
        job_id = __import__("json").loads(out.err.strip().splitlines()[-1])["job_id"]
        assert main(["status", job_id], registry=reg) == 0
        status = __import__("json").loads(capsys.readouterr().out)
    finally:
        get_settings.cache_clear()
    assert status["job"]["status"] == "done"
    assert status["steps"][0]["status"] == "done"
    with db.tx() as conn:
        assert job_status(conn, job_id)["step_counts"] == {"done": 1}
