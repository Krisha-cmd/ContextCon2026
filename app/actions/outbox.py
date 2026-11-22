"""The outbox: every irreversible action goes through here, exactly once.

States: pending_approval -> approved -> executing -> done | failed; pending_approval ->
rejected (terminal). Every transition writes an audit_log row in the same transaction.

execute_ready():
  tx 1  claim approved actions (and executing ones whose lease ran out: crash recovery) with
        FOR UPDATE SKIP LOCKED, mark them executing
  ----  do the side effect (it checks first whether it already happened)
  tx 2  mark done / failed, only if this worker still holds the claim
Two workers never claim the same row; a crash between the side effect and tx 2 leaves the
row executing, and the next claim after lease_s finds the effect already done (409 / sent).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app import db
from app.actions.effects import Effects, Outcome
from app.actions.models import State, payload_hash, validate_payload
from app.config import Settings, get_settings
from app.log import bind, get_logger
from app.runner.audit import audit

log = get_logger(__name__)

STEP = "actions"
TxFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


class InvalidTransition(RuntimeError):  # noqa: N818 - reads as a state-machine verdict
    pass


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _audit(
    conn: psycopg.Connection, row: dict[str, Any], before: Any, after: Any, reason: str
) -> None:
    audit(conn, row["job_id"], STEP, "actions", row["idem_key"], before, after, reason)


def _lock(conn: psycopg.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM actions WHERE idem_key = %s FOR UPDATE", (key,)).fetchone()
    if row is None:
        raise KeyError(f"no action {key!r}")
    return row


def _require(row: dict[str, Any], *states: State) -> None:
    if row["state"] not in states:
        raise InvalidTransition(f"{row['idem_key']}: {row['state']} -> needs {list(states)}")


# ---- proposing and approving ---------------------------------------------------------------
def propose(
    conn: psycopg.Connection,
    *,
    key: str,
    job_id: Any,
    account_id: Any,
    kind: str,
    payload: dict[str, Any],
) -> bool:
    """Queue an action for approval. Idempotent: an existing key is left as it is."""
    clean = validate_payload(kind, payload)
    row = conn.execute(
        "INSERT INTO actions (idem_key, job_id, account_id, type, payload, state) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idem_key) DO NOTHING RETURNING *",
        (key, job_id, account_id, kind, Jsonb(clean), State.pending_approval),
    ).fetchone()
    if row is not None:
        _audit(conn, row, None, {"state": State.pending_approval, "payload": clean}, "proposed")
    return row is not None


def _decide(
    conn: psycopg.Connection,
    row: dict[str, Any],
    decision: str,
    by_user: str,
    draft: str | None,
    final: str | None,
    reason: str | None,
    phash: str | None,
) -> None:
    conn.execute(
        "INSERT INTO approvals (action_key, decision, draft_text, final_text, reason, by_user, "
        "payload_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (row["idem_key"], decision, draft, final, reason, by_user, phash),
    )


def _text(payload: dict[str, Any]) -> str | None:
    return payload.get("body") or payload.get("description")


def approve(
    conn: psycopg.Connection,
    key: str,
    by_user: str,
    *,
    payload: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """A person approves, optionally with an edited payload. That exact payload is what runs."""
    row = _lock(conn, key)
    _require(row, State.pending_approval)
    final = validate_payload(row["type"], payload if payload is not None else row["payload"])
    phash = payload_hash(final)
    conn.execute(
        "UPDATE actions SET payload = %s, state = %s, updated_at = now() WHERE idem_key = %s",
        (Jsonb(final), State.approved, key),
    )
    _decide(conn, row, "approved", by_user, _text(row["payload"]), _text(final), reason, phash)
    _audit(
        conn,
        row,
        {"state": row["state"], "payload": row["payload"]},
        {"state": State.approved, "payload": final, "payload_hash": phash},
        f"approved by {by_user}",
    )


def reject(conn: psycopg.Connection, key: str, by_user: str, reason: str) -> None:
    if not reason:
        raise ValueError("a rejection needs a reason")
    row = _lock(conn, key)
    _require(row, State.pending_approval)
    conn.execute(
        "UPDATE actions SET state = %s, updated_at = now() WHERE idem_key = %s",
        (State.rejected, key),
    )
    _decide(conn, row, "rejected", by_user, _text(row["payload"]), None, reason, None)
    _audit(
        conn,
        row,
        {"state": row["state"]},
        {"state": State.rejected},
        f"rejected by {by_user}: {reason}",
    )


# ---- executing ----------------------------------------------------------------------------
_READY = """
SELECT a.*, ap.payload_hash AS approved_hash
FROM actions a
JOIN LATERAL (
    SELECT payload_hash FROM approvals p
    WHERE p.action_key = a.idem_key AND p.decision = 'approved'
    ORDER BY p.id DESC LIMIT 1
) ap ON true
WHERE a.state = 'approved'
   OR (a.state = 'executing' AND a.updated_at < now() - make_interval(secs => %(lease)s))
ORDER BY a.updated_at
LIMIT %(n)s
FOR UPDATE OF a SKIP LOCKED
"""


@dataclass(frozen=True)
class Claimed:
    action: dict[str, Any]
    owner: str


def claim(conn: psycopg.Connection, s: Settings, owner: str) -> list[Claimed]:
    """tx 1: lock ready rows (skipping ones another worker holds) and mark them executing."""
    rows = conn.execute(_READY, {"lease": s.actions.lease_s, "n": s.actions.batch_size}).fetchall()
    out = []
    for row in rows:
        if row["attempts"] >= s.actions.max_attempts:
            _finish_row(
                conn,
                row,
                Outcome(
                    "failed", None, f"gave up after {row['attempts']} attempts: {row['last_error']}"
                ),
            )
            continue
        conn.execute(
            "UPDATE actions SET state = %s, attempts = attempts + 1, claimed_by = %s, "
            "updated_at = now() WHERE idem_key = %s",
            (State.executing, owner, row["idem_key"]),
        )
        reason = "claimed" if row["state"] == State.approved else "reclaimed after lease expiry"
        _audit(
            conn,
            row,
            {"state": row["state"], "claimed_by": row["claimed_by"]},
            {"state": State.executing, "claimed_by": owner, "attempt": row["attempts"] + 1},
            reason,
        )
        out.append(Claimed({**row, "state": State.executing}, owner))
    return out


def _finish_row(conn: psycopg.Connection, row: dict[str, Any], outcome: Outcome) -> None:
    state = State.done if outcome.status == "done" else State.failed
    conn.execute(
        "UPDATE actions SET state = %s, result = %s, last_error = %s, updated_at = now() "
        "WHERE idem_key = %s",
        (state, Jsonb(outcome.result) if outcome.result else None, outcome.error, row["idem_key"]),
    )
    _audit(
        conn,
        row,
        {"state": row["state"]},
        {"state": state, "result": outcome.result, "error": outcome.error},
        outcome.error or "side effect done",
    )


def finish(conn: psycopg.Connection, c: Claimed, outcome: Outcome) -> bool:
    """tx 2: record the outcome, only if this worker still holds the claim."""
    row = conn.execute(
        "SELECT * FROM actions WHERE idem_key = %s AND state = %s AND claimed_by = %s FOR UPDATE",
        (c.action["idem_key"], State.executing, c.owner),
    ).fetchone()
    if row is None:
        log.warning("claim lost before finish", extra={"idem_key": c.action["idem_key"]})
        return False
    if outcome.status == "retry":
        # Unknown whether it happened: stay executing; the lease expiry re-claims it and the
        # effect's check-first path decides. updated_at is not touched, so the lease runs.
        conn.execute(
            "UPDATE actions SET last_error = %s WHERE idem_key = %s",
            (outcome.error, row["idem_key"]),
        )
        _audit(
            conn,
            row,
            {"state": row["state"]},
            {"state": row["state"], "error": outcome.error},
            f"will retry after lease: {outcome.error}",
        )
        return True
    _finish_row(conn, row, outcome)
    return True


def check_approved(c: Claimed) -> Outcome | None:
    """The payload about to run must be byte-for-byte what was approved."""
    if c.action.get("approved_hash") != payload_hash(c.action["payload"]):
        return Outcome("failed", None, "payload differs from the approved payload")
    return None


def _in_tx(tx: TxFactory, fn: Callable[[psycopg.Connection], Any]) -> Any:
    with tx() as conn:
        return fn(conn)


async def execute_ready(
    effects: Effects,
    *,
    settings: Settings | None = None,
    owner: str | None = None,
    tx: TxFactory = db.tx,
) -> list[Outcome]:
    s = settings or get_settings()
    owner = owner or default_owner()
    claimed = await asyncio.to_thread(_in_tx, tx, lambda conn: claim(conn, s, owner))
    outcomes = []
    for c in claimed:
        with bind(
            job_id=str(c.action["job_id"]), account_id=str(c.action["account_id"]), step=STEP
        ):
            outcome = check_approved(c) or await _run(effects, c)
            await asyncio.to_thread(_in_tx, tx, lambda conn, c=c, o=outcome: finish(conn, c, o))
            log.info(
                "action executed",
                extra={
                    "idem_key": c.action["idem_key"],
                    "type": c.action["type"],
                    "outcome": outcome.status,
                },
            )
        outcomes.append(outcome)
    return outcomes


async def _run(effects: Effects, c: Claimed) -> Outcome:
    try:
        return await effects.run(c.action)
    except Exception as exc:  # unknown: leave executing, the lease decides
        log.warning("side effect raised", exc_info=True)
        return Outcome("retry", None, f"{type(exc).__name__}: {exc}")
