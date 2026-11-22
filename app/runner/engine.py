"""Durable step machine: claim, commit, fail, skip, and schedule the next steps.

Invariants
- A step's output, its status change, its buffered audit rows and its side effects (new
  accounts, drop/hold decisions) commit in ONE transaction, and only if this worker still
  owns the lease. A crash before that commit leaves nothing behind, so a re-run is clean.
- Lock order is always: own step row, then the job row. Every scheduling decision happens
  while holding the job row lock, so per-job barriers and "job done" checks are race-free.
- Steps are scheduled lazily: when a step finishes, the next step's rows are inserted in the
  same transaction (fan-out per active account; job-level steps wait for all accounts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.config import Settings
from app.runner.audit import audit
from app.runner.context import ACTIVE, HELD, StepContext
from app.runner.registry import Registry, StepDef

TERMINAL = ("done", "skipped", "failed")
STOP_BUDGET, STOP_TIME = "budget_exceeded", "time_limit"


class LeaseLostError(RuntimeError):
    """Another worker owns this step now (our lease expired and it was reclaimed)."""


@dataclass(frozen=True)
class Claim:
    step: dict[str, Any]
    job: dict[str, Any]
    account: dict[str, Any] | None
    stepdef: StepDef
    owner: str


@dataclass
class Engine:
    conn: Connection
    settings: Settings
    registry: Registry

    # ---- helpers ------------------------------------------------------------------------
    def is_optional(self, sd: StepDef) -> bool:
        return sd.name in self.settings.runner.optional_steps

    def _audit(self, job: dict, step: str | None, tbl: str, rid: Any, before: Any, after: Any,
               reason: str) -> None:  # fmt: skip
        audit(self.conn, job["id"], step, tbl, rid, before, after, reason)

    def _one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return self.conn.execute(sql, params).fetchone()

    def lock_job(self, job_id: Any) -> dict[str, Any]:
        return self._one("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))

    def lock_own_step(self, step_id: int, owner: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM steps WHERE id = %s FOR UPDATE", (step_id,))
        if row is None or row["status"] != "running" or row["lease_owner"] != owner:
            raise LeaseLostError(f"step {step_id} is no longer leased by {owner}")
        return row

    def account(self, account_id: Any) -> dict[str, Any] | None:
        if account_id is None:
            return None
        return self._one("SELECT * FROM accounts WHERE id = %s", (account_id,))

    def minutes_limit(self, job: dict) -> float:
        lim = (job.get("limits") or {}).get("max_minutes_per_job")
        return float(lim) if lim else float(self.settings.limits.max_minutes_per_job)

    def time_exceeded(self, job: dict) -> bool:
        row = self._one(
            "SELECT now() - created_at > make_interval(secs => %s) AS over FROM jobs WHERE id = %s",
            (self.minutes_limit(job) * 60, job["id"]),
        )
        return bool(row and row["over"])

    # ---- claiming -----------------------------------------------------------------------
    _CANDIDATE = """
        SELECT s.* FROM steps s JOIN jobs j ON j.id = s.job_id
        WHERE s.status IN ('pending', 'running')
          AND (s.lease_until IS NULL OR s.lease_until < now())
          AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= now())
          AND j.status = 'running'
          AND s.step = ANY(%(steps)s)
          AND (%(job_id)s::uuid IS NULL OR s.job_id = %(job_id)s::uuid)
        ORDER BY s.priority, s.id
        FOR UPDATE OF s SKIP LOCKED
        LIMIT 1
    """

    def claim_one(self, owner: str, job_id: Any = None) -> Claim | bool | None:
        """Try to claim one step. Returns a Claim, None (nothing claimable), or True when a
        candidate was resolved without running it (skipped / dead-lettered); call again."""
        params = {"steps": self.registry.names(), "job_id": job_id}
        row = self._one(self._CANDIDATE, params)
        if row is None:
            return None
        job = self.lock_job(row["job_id"])
        sd = self.registry.get(row["step"])
        self.enforce_time_limit(job, sd.name)
        row = self._one("SELECT * FROM steps WHERE id = %s", (row["id"],))
        if row["status"] in TERMINAL:  # the time-limit stop just skipped it
            self.finish_if_done(job)
            return True
        if self.is_optional(sd) and job["stop_optional"]:
            self.mark_skipped(job, row, sd, f"optional step skipped: {job['stop_optional']}")
            self.finish_if_done(job)
            return True
        if row["attempts"] >= self.settings.runner.max_attempts:
            reason = f"lease expired after {row['attempts']} attempts (worker died mid-step?)"
            self.dead_letter(job, row, sd, reason)
            self.finish_if_done(job)
            return True
        step = self._one(
            "UPDATE steps SET status = 'running', lease_owner = %s, "
            "lease_until = now() + make_interval(secs => %s), attempts = attempts + 1, "
            "updated_at = now() WHERE id = %s RETURNING *",
            (owner, self.settings.runner.lease_s, row["id"]),
        )
        return Claim(step, job, self.account(step["account_id"]), sd, owner)

    # ---- outcomes -----------------------------------------------------------------------
    def complete(self, claim: Claim, output: Any, ctx: StepContext) -> None:
        self.lock_own_step(claim.step["id"], claim.owner)
        job = self.lock_job(claim.job["id"])
        sd, name = claim.stepdef, claim.stepdef.name
        self._insert_accounts(job, name, ctx)
        for w in ctx.writes:  # buffered step writes; their audit rows flush below
            self.conn.execute(w.sql, w.params)
        self._apply_dispositions(job, name, ctx)
        self.conn.execute(
            "UPDATE steps SET status = 'done', output = %s, lease_owner = NULL, "
            "lease_until = NULL, last_error = NULL, updated_at = now() WHERE id = %s",
            (Jsonb(output), claim.step["id"]),
        )
        after = {"status": "done", "attempts": claim.step["attempts"]}
        self._audit(job, name, "steps", claim.step["id"], {"status": "running"}, after,
                    "step completed")  # fmt: skip
        ctx.audit.flush(self.conn, job["id"], name)
        self.enforce_time_limit(job, name)
        self.advance(job, sd, claim.step["account_id"])
        self.finish_if_done(job)

    def fail(self, claim: Claim, error: str) -> None:
        step = self.lock_own_step(claim.step["id"], claim.owner)
        job = self.lock_job(claim.job["id"])
        if step["attempts"] >= self.settings.runner.max_attempts:
            self.dead_letter(job, step, claim.stepdef, error)
        else:
            self._schedule_retry(job, step, claim.stepdef, error)
        self.finish_if_done(job)

    def budget_exceeded(self, claim: Claim, detail: str) -> None:
        step = self.lock_own_step(claim.step["id"], claim.owner)
        job = self.lock_job(claim.job["id"])
        sd = claim.stepdef
        self.stop_optional(job, STOP_BUDGET, sd.name, detail)
        if sd.per_account and not self.is_optional(sd):
            self.set_account_status(job, sd.name, step["account_id"], HELD,
                                    f"budget exceeded during {sd.name}: {detail}")  # fmt: skip
        self.mark_skipped(job, step, sd, f"budget exceeded: {detail}")
        self.finish_if_done(job)

    def release(self, claim: Claim) -> bool:
        """Give the lease back (clean shutdown). The attempt is not counted as a failure."""
        row = self._one(
            "UPDATE steps SET status = 'pending', lease_owner = NULL, lease_until = NULL, "
            "attempts = GREATEST(attempts - 1, 0), updated_at = now() "
            "WHERE id = %s AND lease_owner = %s AND status = 'running' RETURNING id",
            (claim.step["id"], claim.owner),
        )
        if row:
            self._audit(claim.job, claim.stepdef.name, "steps", row["id"], {"status": "running"},
                        {"status": "pending"}, "lease released on shutdown")  # fmt: skip
        return row is not None

    def heartbeat(self, step_id: int, owner: str) -> bool:
        row = self._one(
            "UPDATE steps SET lease_until = now() + make_interval(secs => %s) "
            "WHERE id = %s AND lease_owner = %s AND status = 'running' RETURNING id",
            (self.settings.runner.lease_s, step_id, owner),
        )
        return row is not None

    # ---- terminal transitions -----------------------------------------------------------
    def mark_skipped(self, job: dict, step: dict, sd: StepDef, reason: str) -> None:
        self.conn.execute(
            "UPDATE steps SET status = 'skipped', output = %s, lease_owner = NULL, "
            "lease_until = NULL, updated_at = now() WHERE id = %s",
            (Jsonb({"skipped": reason}), step["id"]),
        )
        self._audit(job, sd.name, "steps", step["id"], {"status": step["status"]},
                    {"status": "skipped"}, reason)  # fmt: skip
        self.advance(job, sd, step["account_id"])

    def dead_letter(self, job: dict, step: dict, sd: StepDef, error: str) -> None:
        self.conn.execute(
            "UPDATE steps SET status = 'failed', last_error = %s, lease_owner = NULL, "
            "lease_until = NULL, updated_at = now() WHERE id = %s",
            (error, step["id"]),
        )
        self.conn.execute(
            "INSERT INTO dead_letter (step_id, error, attempts) VALUES (%s, %s, %s)",
            (step["id"], error, step["attempts"]),
        )
        self._audit(job, sd.name, "steps", step["id"], {"status": step["status"]},
                    {"status": "failed", "attempts": step["attempts"]},
                    f"dead-lettered after {step['attempts']} attempts: {error}")  # fmt: skip
        if sd.per_account:
            self.set_account_status(job, sd.name, step["account_id"], HELD,
                                    f"step failed: {sd.name}: {error}")  # fmt: skip
            self.advance(job, sd, step["account_id"])
        elif self.is_optional(sd):
            self.advance(job, sd, None)
        else:
            self.set_job_status(job, "failed", f"core step failed: {sd.name}: {error}", sd.name)

    def _schedule_retry(self, job: dict, step: dict, sd: StepDef, error: str) -> None:
        r = self.settings.runner
        delay = min(r.retry_backoff_max_s, r.retry_backoff_base_s * 2 ** (step["attempts"] - 1))
        self.conn.execute(
            "UPDATE steps SET status = 'pending', lease_owner = NULL, lease_until = NULL, "
            "last_error = %s, next_attempt_at = now() + make_interval(secs => %s), "
            "updated_at = now() WHERE id = %s",
            (error, delay, step["id"]),
        )
        self._audit(job, sd.name, "steps", step["id"], {"status": "running"},
                    {"status": "pending", "retry_in_s": delay, "attempts": step["attempts"]},
                    f"attempt {step['attempts']} failed: {error}")  # fmt: skip

    # ---- accounts & job -----------------------------------------------------------------
    def _insert_accounts(self, job: dict, step: str, ctx: StepContext) -> None:
        for a in ctx.new_accounts:
            row = self._one(
                "INSERT INTO accounts (job_id, entity_id, name, domain, data) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (job["id"], a.entity_id, a.name, a.domain, Jsonb(a.data)),
            )
            self._audit(job, step, "accounts", row["id"], None,
                        {"name": a.name, "domain": a.domain, "status": ACTIVE},
                        "account discovered")  # fmt: skip

    def _apply_dispositions(self, job: dict, step: str, ctx: StepContext) -> None:
        for d in ctx.dispositions:
            self.set_account_status(job, step, d.account_id, d.status, d.reason, d.evidence_ids)

    def set_account_status(self, job: dict, step: str, account_id: Any, status: str,
                           reason: str, evidence_ids: tuple[str, ...] = ()) -> None:  # fmt: skip
        before = self._one(
            "SELECT status, status_reason FROM accounts WHERE id = %s AND job_id = %s FOR UPDATE",
            (account_id, job["id"]),
        )
        if before is None:
            raise ValueError(f"account {account_id} is not in job {job['id']}")
        self.conn.execute(
            "UPDATE accounts SET status = %s, status_reason = %s, status_step = %s WHERE id = %s",
            (status, reason, step, account_id),
        )
        audit(self.conn, job["id"], step, "accounts", account_id, dict(before),
              {"status": status, "status_reason": reason}, reason, evidence_ids)  # fmt: skip

    def set_job_status(self, job: dict, status: str, reason: str, step: str | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = %s, updated_at = now() WHERE id = %s", (status, job["id"])
        )
        self._audit(job, step, "jobs", job["id"], {"status": job["status"]},
                    {"status": status}, reason)  # fmt: skip
        job["status"] = status

    def stop_optional(self, job: dict, reason: str, step: str | None, detail: str = "") -> None:
        """Record why optional work stops, and skip optional steps that are still pending."""
        if job["stop_optional"]:
            return
        self.conn.execute(
            "UPDATE jobs SET stop_optional = %s, updated_at = now() WHERE id = %s",
            (reason, job["id"]),
        )
        why = f"optional steps stopped: {reason} {detail}".strip()
        self._audit(job, step, "jobs", job["id"], {"stop_optional": None},
                    {"stop_optional": reason}, why)  # fmt: skip
        job["stop_optional"] = reason
        pending = self.conn.execute(
            "SELECT * FROM steps WHERE job_id = %s AND status = 'pending' AND step = ANY(%s) "
            "ORDER BY priority, id FOR UPDATE SKIP LOCKED",
            (job["id"], list(self.settings.runner.optional_steps)),
        ).fetchall()
        for row in pending:
            if row["step"] in self.registry.names():
                self.mark_skipped(job, row, self.registry.get(row["step"]),
                                  f"optional step skipped: {reason}")  # fmt: skip

    def enforce_time_limit(self, job: dict, step: str | None) -> None:
        if not job["stop_optional"] and self.time_exceeded(job):
            self.stop_optional(job, STOP_TIME, step, f"> {self.minutes_limit(job)} min")

    def finish_if_done(self, job: dict) -> None:
        if job["status"] != "running":
            return
        row = self._one(
            "SELECT count(*) AS n FROM steps WHERE job_id = %s AND status <> ALL(%s)",
            (job["id"], list(TERMINAL)),
        )
        if row["n"] == 0:
            self.set_job_status(job, "done", "all steps finished", None)

    # ---- scheduling ---------------------------------------------------------------------
    def schedule(self, job: dict, sd: StepDef, account_id: Any) -> None:
        """Insert the step row (idempotent). Optional steps after a stop go straight to skipped."""
        skip = job["stop_optional"] if self.is_optional(sd) else None
        row = self._one(
            "INSERT INTO steps (job_id, account_id, step, status, priority) "
            "VALUES (%s, %s, %s, 'pending', %s) ON CONFLICT DO NOTHING RETURNING *",
            (job["id"], account_id, sd.name, sd.order),
        )
        if row is not None and skip:
            self.mark_skipped(job, row, sd, f"optional step skipped: {skip}")

    def advance(self, job: dict, done: StepDef, account_id: Any) -> None:
        """Schedule whatever becomes runnable now that `done` is terminal."""
        nxt = self.registry.after(done.name)
        if done.per_account:
            if nxt is not None and nxt.per_account and self._is_active(account_id):
                self.schedule(job, nxt, account_id)
            self.release_barrier(job, done.order)
            return
        if nxt is None:
            return
        if not nxt.per_account:
            self.schedule(job, nxt, None)
            return
        accounts = self._active_accounts(job["id"])
        for acc in accounts:
            self.schedule(job, nxt, acc)
        if not accounts:
            self.release_barrier(job, nxt.order)  # nobody to run the per-account segment

    def release_barrier(self, job: dict, from_order: int) -> None:
        """Schedule the next job-level step once no per-account work before it is left."""
        m = self.registry.first_job_level_after(from_order)
        if m is None:
            return
        row = self._one(
            "SELECT count(*) AS n FROM steps WHERE job_id = %s AND account_id IS NOT NULL "
            "AND priority < %s AND status <> ALL(%s)",
            (job["id"], m.order, list(TERMINAL)),
        )
        if row["n"] == 0:
            self.schedule(job, m, None)

    def _is_active(self, account_id: Any) -> bool:
        row = self._one("SELECT status FROM accounts WHERE id = %s", (account_id,))
        return bool(row and row["status"] == ACTIVE)

    def _active_accounts(self, job_id: Any) -> list[Any]:
        rows = self.conn.execute(
            "SELECT id FROM accounts WHERE job_id = %s AND status = %s ORDER BY id",
            (job_id, ACTIVE),
        ).fetchall()
        return [r["id"] for r in rows]
