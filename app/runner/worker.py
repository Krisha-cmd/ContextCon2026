"""Worker: claim a step, run it under a heartbeat, commit the outcome. Repeat.

SIGTERM/SIGINT cancel the running step (async steps) and release its lease, so another worker
can take it immediately. A hard kill is safe too: nothing was committed, the lease expires,
and the step is reclaimed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from app import db
from app import gateway as gateway_module
from app.actions.loop import OutboxLoop
from app.config import Settings, get_settings
from app.gateway import BudgetExceeded
from app.log import bind, get_logger, setup_logging
from app.runner.context import StepContext
from app.runner.engine import Claim, Engine, LeaseLostError
from app.runner.heartbeat import Heartbeat
from app.runner.registry import REGISTRY, Registry

log = get_logger(__name__)
FINISHED_JOB_STATUSES = ("done", "failed")


def new_event_loop() -> asyncio.AbstractEventLoop:
    # psycopg async (used by app.gateway) can't run on Windows' ProactorEventLoop.
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self, settings: Settings, registry: Registry, *, job_id: Any = None,
                 owner: str | None = None) -> None:  # fmt: skip
        self.settings = settings
        self.registry = registry
        self.job_id = job_id  # restrict to one job (CLI run/resume); None = any job
        self.owner = owner or default_owner()
        self.stop = threading.Event()
        self.loop = new_event_loop()
        self._task: asyncio.Task | None = None

    # ---- db helpers ---------------------------------------------------------------------
    def _tx(self, fn: Callable[[Engine], Any]) -> Any:
        with db.tx() as conn:
            return fn(Engine(conn, self.settings, self.registry))

    def claim(self) -> Claim | None:
        while True:
            got = self._tx(lambda e: e.claim_one(self.owner, self.job_id))
            if got is not True:  # True = resolved without running; look again
                return got

    def job_finished(self) -> bool:
        with db.tx() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = %s", (self.job_id,)).fetchone()
        return row is None or row["status"] in FINISHED_JOB_STATUSES

    # ---- loop ---------------------------------------------------------------------------
    def run(self, *, until_job_done: bool = False, max_steps: int | None = None) -> int:
        """Claim and run steps until stopped (or the job finishes / max_steps). Returns count."""
        done = 0
        while not self.stop.is_set():
            if until_job_done and self.job_finished():
                break
            if max_steps is not None and done >= max_steps:
                break
            if self.run_once():
                done += 1
            else:
                self.stop.wait(self.settings.runner.poll_interval_s)
        return done

    def run_once(self) -> bool:
        claim = self.claim()
        if claim is None:
            return False
        self.execute(claim)
        return True

    def request_stop(self, *_args: Any) -> None:
        """Signal handler: stop the loop and cancel the running async step."""
        self.stop.set()
        task = self._task
        if task is not None and not task.done():
            self.loop.call_soon_threadsafe(task.cancel)

    # ---- one step -----------------------------------------------------------------------
    def execute(self, claim: Claim) -> None:
        sd = claim.stepdef
        ctx = StepContext(claim.job, claim.account, self.settings, gateway_module, db)
        acct = claim.account["id"] if claim.account else None
        with bind(job_id=str(claim.job["id"]), account_id=acct and str(acct), step=sd.name):
            t0 = time.perf_counter()
            log.info("step start", extra={"attempt": claim.step["attempts"]})
            outcome = self._run_step(claim, ctx)
            ms = round((time.perf_counter() - t0) * 1000)
            log.info("step end", extra={"outcome": outcome, "ms": ms})

    def _run_step(self, claim: Claim, ctx: StepContext) -> str:
        beat = lambda: self._tx(lambda e: e.heartbeat(claim.step["id"], claim.owner))  # noqa: E731
        try:
            with Heartbeat(beat, self.settings.runner.heartbeat_s):
                output = self._invoke(claim.stepdef.fn, ctx)
            json.dumps(output)  # must be JSON-serializable; TypeError -> failure path
        except asyncio.CancelledError:
            self._commit(lambda e: e.release(claim))
            return "released"
        except BudgetExceeded as exc:
            detail = str(exc)
            self._commit(lambda e: e.budget_exceeded(claim, detail))
            return "budget_exceeded"
        except Exception as exc:
            log.warning("step failed", exc_info=True)
            error = f"{type(exc).__name__}: {exc}"
            self._commit(lambda e: e.fail(claim, error))
            return "failed"
        finally:
            self._task = None
        self._commit(lambda e: e.complete(claim, output, ctx))
        return "done"

    def _invoke(self, fn: Callable[[StepContext], Any], ctx: StepContext) -> Any:
        result = fn(ctx)
        if not inspect.isawaitable(result):
            return result
        self._task = self.loop.create_task(result)
        return self.loop.run_until_complete(self._task)

    def _commit(self, fn: Callable[[Engine], Any]) -> None:
        try:
            self._tx(fn)
        except LeaseLostError as exc:
            log.warning("commit refused, lease lost", extra={"error": str(exc)})

    def close(self) -> None:
        try:
            self.loop.run_until_complete(gateway_module.close())
        finally:
            self.loop.close()


def main() -> None:
    import app.runner.pipeline  # noqa: F401  (registers the pipeline steps)

    settings = get_settings()
    setup_logging(settings.log.level)
    db.init_db(settings)
    worker = Worker(settings, REGISTRY)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, worker.request_stop)
    outbox = OutboxLoop(settings, worker.stop)  # same stop event: SIGTERM stops both
    outbox.start()
    with bind(step="worker"):
        log.info("worker started", extra={"owner": worker.owner, "steps": REGISTRY.names()})
        worker.run()
        log.info("worker stopping")
    worker.stop.set()
    outbox.join(timeout=settings.actions.poll_interval_s * 2)
    worker.close()
    db.close_pool()


if __name__ == "__main__":
    main()
