"""Create, resume and inspect jobs."""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.config import Settings
from app.gateway.hashing import canonical_json
from app.runner.audit import audit
from app.runner.engine import Engine
from app.runner.registry import Registry


def create_job(conn: Connection, settings: Settings, registry: Registry, spec: dict[str, Any],
               client_id: str, limits: dict[str, Any] | None = None) -> Any:  # fmt: skip
    """Insert the job and its first step. `limits` (e.g. from the request form) can only
    tighten the configured limits, never raise them."""
    first = registry.first()
    if first.per_account:
        raise ValueError(f"first step {first.name!r} must be job-level")
    snapshot = hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
    job = conn.execute(
        "INSERT INTO jobs (client_id, spec, status, limits, input_snapshot_hash) "
        "VALUES (%s, %s, 'running', %s, %s) RETURNING *",
        (client_id, Jsonb(spec), Jsonb(effective_limits(settings, limits)), snapshot),
    ).fetchone()
    audit(conn, job["id"], None, "jobs", job["id"], None,
          {"status": "running", "input_snapshot_hash": snapshot}, "job created")  # fmt: skip
    Engine(conn, settings, registry).schedule(job, first, None)
    return job["id"]


def effective_limits(settings: Settings, overrides: dict[str, Any] | None) -> dict[str, Any]:
    base = settings.limits.model_dump()
    for key, value in (overrides or {}).items():
        if key in base and isinstance(value, int | float) and value > 0:
            base[key] = min(base[key], value)
    return base


def resume_job(conn: Connection, settings: Settings, registry: Registry, job_id: Any) -> str:
    """Make a job runnable again. Failed core job-level steps get a fresh set of attempts."""
    engine = Engine(conn, settings, registry)
    job = engine.lock_job(job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    if job["status"] == "done":
        return "done"
    failed = conn.execute(
        "UPDATE steps SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
        "updated_at = now() WHERE job_id = %s AND account_id IS NULL AND status = 'failed' "
        "RETURNING id, step",
        (job_id,),
    ).fetchall()
    for row in failed:
        audit(conn, job_id, row["step"], "steps", row["id"], {"status": "failed"},
              {"status": "pending", "attempts": 0}, "requeued on resume")  # fmt: skip
    if job["status"] != "running":
        engine.set_job_status(job, "running", "job resumed", None)
    return "running"


def job_status(conn: Connection, job_id: Any) -> dict[str, Any]:
    job = conn.execute(
        "SELECT id, client_id, status, stop_optional, created_at, "
        "extract(epoch FROM now() - created_at)::int AS elapsed_s FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    if job is None:
        raise LookupError(f"job {job_id} not found")
    steps = conn.execute(
        "SELECT step, account_id, status, attempts, last_error, lease_owner, lease_until "
        "FROM steps WHERE job_id = %s ORDER BY priority, id",
        (job_id,),
    ).fetchall()
    accounts = conn.execute(
        "SELECT id, name, domain, status, status_reason, status_step FROM accounts "
        "WHERE job_id = %s ORDER BY name",
        (job_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"job": job, "step_counts": counts, "steps": steps, "accounts": accounts}
