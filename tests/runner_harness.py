"""Subprocess entry for the kill-and-restart test.

    python -m tests.runner_harness <job_id> <run_log>

Three dummy steps: s1 (job-level, discovers accounts A and B), s2 (per account), s3
(job-level). If env KILL_MARKER is set and that file does not exist yet, s2 creates it and
hard-kills the process with os._exit, mid-step. Every step execution is appended to
<run_log>, so the test can count re-runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app import db
from app.config import get_settings
from app.runner.context import StepContext
from app.runner.registry import Registry
from app.runner.worker import Worker

HARD_KILL_EXIT = 137
REG = Registry()


def _log(line: str) -> None:
    with Path(os.environ["RUN_LOG"]).open("a", encoding="utf-8") as f:
        f.write(line + "\n")


@REG.step("s1", per_account=False, order=1)
def s1(ctx: StepContext) -> dict:
    _log("s1")
    ctx.add_account("A", "a.example")
    ctx.add_account("B", "b.example")
    ctx.audit("custom", "s1", None, {"found": 2}, "dummy audit from s1")
    return {"accounts": 2}


@REG.step("s2", per_account=True, order=2)
async def s2(ctx: StepContext) -> dict:
    name = ctx.account["name"]
    _log(f"s2:{name}")
    marker = os.environ.get("KILL_MARKER")
    if marker and not Path(marker).exists():
        Path(marker).write_text(name, encoding="utf-8")
        os._exit(HARD_KILL_EXIT)  # hard kill: no cleanup, no lease release
    ctx.audit("custom", ctx.account_id, None, {"checked": name}, "dummy audit from s2")
    return {"account": name}


@REG.step("s3", per_account=False, order=3)
def s3(ctx: StepContext) -> dict:
    _log("s3")
    return {"done": True}


def main() -> None:
    job_id, run_log = sys.argv[1], sys.argv[2]
    os.environ["RUN_LOG"] = run_log
    settings = get_settings()
    db.open_pool(settings)
    worker = Worker(settings, REG, job_id=job_id)
    try:
        worker.run(until_job_done=True)
    finally:
        worker.close()
        db.close_pool()


if __name__ == "__main__":
    main()
