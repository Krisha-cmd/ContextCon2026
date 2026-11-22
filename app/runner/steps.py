"""Runner step registry. Each step takes the job context plus earlier steps' outputs and
returns a JSON-able dict, which the runner stores in steps.output (see save_step_output).

The claim/lease loop lives in app/runner/worker.py; it is not built yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.discovery.dedup import dedup
from app.discovery.doors import Deps, discover
from app.discovery.models import Candidate, Cluster
from app.discovery.pre_rank import pre_rank
from app.spec.models import RunSpec

STEP_DONE = "done"


@dataclass
class StepContext:
    job_id: Any
    spec: RunSpec
    settings: Settings = field(default_factory=get_settings)
    deps: Deps = field(default_factory=Deps)
    today: date = field(default_factory=date.today)
    budget_credits: float | None = None


Step = Callable[[StepContext, dict[str, dict[str, Any]]], Awaitable[dict[str, Any]]]


async def discover_step(ctx: StepContext, _: dict[str, dict[str, Any]]) -> dict[str, Any]:
    found = await discover(ctx.job_id, ctx.spec, ctx.deps, ctx.settings)
    return {"candidates": [c.model_dump(mode="json") for c in found]}


async def dedup_step(ctx: StepContext, inputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cands = [Candidate.model_validate(c) for c in inputs["discover"]["candidates"]]
    return {"clusters": [c.model_dump(mode="json") for c in dedup(cands, ctx.settings)]}


async def pre_rank_step(ctx: StepContext, inputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    clusters = [Cluster.model_validate(c) for c in inputs["dedup"]["clusters"]]
    result = pre_rank(
        clusters, ctx.spec, today=ctx.today, budget_credits=ctx.budget_credits,
        settings=ctx.settings,
    )  # fmt: skip
    return result.model_dump(mode="json")


# Order matters: each step reads the outputs of the ones before it.
STEPS: dict[str, Step] = {
    "discover": discover_step,
    "dedup": dedup_step,
    "pre_rank": pre_rank_step,
}

_UPSERT = """
INSERT INTO steps (job_id, account_id, step, status, output, updated_at)
VALUES (%(job)s, NULL, %(step)s, %(status)s, %(output)s, now())
ON CONFLICT (job_id, account_id, step)
DO UPDATE SET status = EXCLUDED.status, output = EXCLUDED.output, updated_at = now()
RETURNING id
"""
_BEFORE = "SELECT status, output FROM steps WHERE job_id = %s AND account_id IS NULL AND step = %s"
_AUDIT = """
INSERT INTO audit_log (job_id, step, tbl, record_id, before, after, reason, evidence_ids)
VALUES (%s, %s, 'steps', %s, %s, %s, %s, %s)
"""


def evidence_ids(output: Any) -> list[str]:
    """Every evidence_id found anywhere in a step output (for the audit row)."""
    found: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("evidence_id"), str):
                found.add(x["evidence_id"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(output)
    return sorted(found)


def save_step_output(
    conn: psycopg.Connection, job_id: Any, step: str, output: dict[str, Any]
) -> int:
    """Store a job-level step's output and mark it done, with its audit row, in one tx."""
    with conn.transaction():
        before = conn.execute(_BEFORE, (job_id, step)).fetchone()
        row = conn.execute(
            _UPSERT, {"job": job_id, "step": step, "status": STEP_DONE, "output": Jsonb(output)}
        ).fetchone()
        conn.execute(
            _AUDIT,
            (job_id, step, str(row["id"]), Jsonb(before) if before else None,
             Jsonb({"status": STEP_DONE, "output": output}), f"step {step} done",
             evidence_ids(output)),
        )  # fmt: skip
    return int(row["id"])
